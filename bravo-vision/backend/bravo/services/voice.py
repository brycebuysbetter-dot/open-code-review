"""AI voice calls: request -> human approval -> gated placement -> bounded, compliant conversation."""
import json
import re
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bravo import approvals, audit
from bravo.config import get_settings
from bravo.crypto import mask_phone
from bravo.db import utcnow
from bravo.enums import ActorType, ApprovalKind, LeadStatus
from bravo.integrations import registry
from bravo.integrations.base import PermanentSendError
from bravo.models import Approval, Lead, Task, VoiceCall
from bravo.retry import TransientError
from bravo.services import compliance, crm_sync
from bravo.services.hours import is_contact_allowed, next_contact_time
from bravo.services.optout import is_opted_out, record_opt_out

STOP_SPEECH = re.compile(r"\b(stop (?:calling|contacting)|do not call|don'?t call|remove (?:me|my number)|take me off|not interested|leave me alone|unsubscribe)\b", re.I)
GOODBYE_STOP = "Understood. I've removed you from our list and you won't be called again. Goodbye."
GOODBYE_HUMAN = "Thanks for your time. A member of our team will follow up with you directly. Goodbye."
OPEN_STATUSES = ("pending_approval", "scheduled", "queued", "in_progress")


class VoiceError(Exception):
    pass


def opening_line(lead: Lead) -> str:
    name = (lead.seller.name or "").split()[0] if lead.seller and lead.seller.name else ""
    hi = f"Hi {name}," if name else "Hello,"
    return (f"{hi} this is an automated assistant calling on behalf of {get_settings().business_name}. "
            "You recently asked about selling your property. Is now an okay time for a few quick questions? "
            "You can say stop at any time and we won't call again.")


def request_call(session: Session, lead: Lead, *, goal: str, requested_by: str, actor_type: ActorType = ActorType.USER) -> VoiceCall:
    problems = []
    phone = lead.seller.phone if lead.seller else None
    if not phone:
        problems.append("lead has no phone number")
    if lead.status == LeadStatus.DO_NOT_CONTACT or (phone and (is_opted_out(session, "voice", phone) or is_opted_out(session, "sms", phone))):
        problems.append("lead opted out / do-not-contact")
    if not lead.voice_consent:
        problems.append("no recorded voice consent for this lead (POST /leads/{id}/consent)")
    if session.scalar(select(VoiceCall.id).where(VoiceCall.lead_id == lead.id, VoiceCall.status.in_(OPEN_STATUSES))):
        problems.append("a call for this lead is already pending or in progress")
    if problems:
        audit.log_event(session, actor=requested_by, actor_type=actor_type, event="call.request_blocked", entity_type="lead",
                        entity_id=lead.id, data={"problems": problems})
        raise VoiceError("; ".join(problems))
    call = VoiceCall(lead_id=lead.id, goal=goal[:300], opening=opening_line(lead), created_by=requested_by)
    session.add(call)
    session.flush()
    appr = approvals.request_approval(
        session, kind=ApprovalKind.CALL, requested_by=requested_by, actor_type=actor_type, entity_type="call", entity_id=call.id,
        summary=f"AI voice call to {mask_phone(phone)} (lead #{lead.id}): {goal[:120]}",
        payload={"call_id": call.id, "lead_id": lead.id, "to": mask_phone(phone), "goal": call.goal, "opening": call.opening})
    call.approval_id = appr.id
    return call


def _calls_today(session: Session, lead_id: int, since: datetime) -> int:
    return session.scalar(select(func.count(VoiceCall.id)).where(
        VoiceCall.lead_id == lead_id, VoiceCall.twilio_sid.is_not(None), VoiceCall.created_at >= since)) or 0


def attempt_call(session: Session, call: VoiceCall, now: datetime | None = None) -> dict:
    s = get_settings()
    now = now or utcnow()
    lead = session.get(Lead, call.lead_id)
    phone = lead.seller.phone if lead.seller else None
    state = lead.property.state if lead.property else None

    def block(reason: str) -> dict:
        call.status, call.error = "blocked", reason
        audit.log_event(session, actor="system", event="call.blocked", entity_type="call", entity_id=call.id, data={"reason": reason})
        return {"status": "blocked", "error": reason}

    if not phone or lead.status == LeadStatus.DO_NOT_CONTACT or is_opted_out(session, "voice", phone) or is_opted_out(session, "sms", phone):
        return block("recipient opted out or has no phone")
    if not lead.voice_consent:
        return block("no recorded voice consent")
    if not s.public_base_url:
        return block("BV_PUBLIC_BASE_URL is not configured; Twilio cannot reach the voice webhooks")
    if not is_contact_allowed(now, state):
        call.status, call.scheduled_for = "scheduled", next_contact_time(now, state)
        return {"status": "scheduled", "scheduled_for": call.scheduled_for.isoformat(), "reason": "outside approved contact hours"}
    if not s.automatic_voice_enabled:
        call.status, call.scheduled_for = "scheduled", now + timedelta(minutes=5)
        audit.log_event(session, actor="system", event="call.held", entity_type="call", entity_id=call.id, data={"reason": "AUTOMATIC_VOICE_ENABLED is false"})
        return {"status": "scheduled", "scheduled_for": call.scheduled_for.isoformat(), "reason": "automatic voice is disabled"}
    from bravo.services.outbound import _start_of_local_day

    if _calls_today(session, lead.id, _start_of_local_day(now, state)) >= 1:
        call.status, call.scheduled_for = "scheduled", next_contact_time(_start_of_local_day(now, state) + timedelta(days=1, hours=1), state)
        return {"status": "scheduled", "scheduled_for": call.scheduled_for.isoformat(), "reason": "one call per lead per day"}
    base = s.public_base_url.rstrip("/")
    try:
        sid = registry.voice().place_call(phone, f"{base}/api/webhooks/twilio/voice-turn?call_id={call.id}",
                                          f"{base}/api/webhooks/twilio/voice-status?call_id={call.id}", s.voice_max_duration_seconds)
    except (PermanentSendError, TransientError, RuntimeError) as exc:
        call.status, call.error = "failed", str(exc)[:500]
        audit.log_event(session, actor="system", event="call.place_failed", entity_type="call", entity_id=call.id, data={"error": str(exc)})
        return {"status": "failed", "error": str(exc)[:500]}
    call.status, call.twilio_sid, call.scheduled_for, call.error = "queued", sid, None, None
    audit.log_event(session, actor="system", event="call.placed", entity_type="call", entity_id=call.id, data={"lead_id": lead.id})
    return {"status": "queued", "sid": sid}


def _exec_call_approval(session: Session, approval: Approval) -> dict:
    call = session.get(VoiceCall, approval.payload["call_id"])
    if call is None or call.opening != approval.payload["opening"]:
        return {"error": "call script differs from what was approved"}
    if call.status != "pending_approval":
        return {"error": f"call is {call.status}"}
    return attempt_call(session, call)


def _reject_call(session: Session, approval: Approval) -> dict:
    call = session.get(VoiceCall, approval.payload.get("call_id", 0))
    if call and call.status == "pending_approval":
        call.status = "rejected"
    return {}


def dispatch_calls(session: Session, now: datetime | None = None, limit: int = 20) -> list[dict]:
    now = now or utcnow()
    due = session.scalars(select(VoiceCall).where(VoiceCall.status == "scheduled", VoiceCall.scheduled_for <= now).limit(limit)).all()
    return [{"call_id": c.id, **attempt_call(session, c, now)} for c in due]


# ---- live conversation -------------------------------------------------------------------------

def _transcript(call: VoiceCall) -> list[dict]:
    return json.loads(call.transcript) if call.transcript else []


def _add(call: VoiceCall, role: str, text: str) -> None:
    call.transcript = json.dumps(_transcript(call) + [{"role": role, "text": text[:1000]}])


def _task(session: Session, call: VoiceCall, title: str, priority: int = 2) -> None:
    session.add(Task(lead_id=call.lead_id, kind="call_review", title=title, priority=priority, due_at=utcnow(), created_by="agent:voice"))


def handle_turn(session: Session, call_id: int, speech: str | None, agent=None) -> dict:
    """Return {"say": str, "hangup": bool}. Never raises into the telephony webhook."""
    s = get_settings()
    call = session.get(VoiceCall, call_id)
    if call is None or call.status not in ("queued", "in_progress"):
        return {"say": "", "hangup": True}
    lead = session.get(Lead, call.lead_id)
    now = utcnow()
    if call.status == "queued":
        call.status, call.started_at = "in_progress", now
        _add(call, "assistant", call.opening)
        return {"say": call.opening, "hangup": False}
    if not speech or not speech.strip():
        return {"say": "Sorry, I didn't catch that. We'll try again another time. Goodbye.", "hangup": True}
    speech = speech.strip()[:1000]
    _add(call, "seller", speech)
    call.turns += 1
    phone = lead.seller.phone if lead.seller else None
    if compliance.classify_keyword(speech) == "stop" or STOP_SPEECH.search(speech):
        if phone:
            record_opt_out(session, "voice", phone, source="voice_request")
            record_opt_out(session, "sms", phone, source="voice_request")
        _add(call, "assistant", GOODBYE_STOP)
        audit.log_event(session, actor="agent:voice", actor_type=ActorType.AGENT, event="call.opt_out", entity_type="call", entity_id=call.id)
        return {"say": GOODBYE_STOP, "hangup": True}
    if call.turns >= s.voice_max_turns or (call.started_at and (now - call.started_at).total_seconds() >= s.voice_max_duration_seconds - 20):
        _add(call, "assistant", GOODBYE_HUMAN)
        _task(session, call, "Voice call reached its limit: follow up personally")
        return {"say": GOODBYE_HUMAN, "hangup": True}
    from bravo.agents.voice import VoiceAgent

    try:
        reply, end = (agent or VoiceAgent()).respond(session, lead.id, call.id, _transcript(call))
    except Exception as exc:  # LLM down, tool crash: hand off to a human rather than improvise
        audit.log_event(session, actor="agent:voice", event="call.agent_error", entity_type="call", entity_id=call.id, data={"error": type(exc).__name__})
        reply, end = GOODBYE_HUMAN, True
        _task(session, call, "Voice agent unavailable mid-call: call back personally", 1)
    problems = compliance.check_message(reply, "voice") if reply else ["empty reply"]
    if problems or len(reply) > 500:
        audit.log_event(session, actor="agent:voice", event="call.reply_rejected", entity_type="call", entity_id=call.id, data={"problems": problems})
        reply, end = GOODBYE_HUMAN, True
        _task(session, call, "Voice reply failed compliance: follow up personally", 1)
    _add(call, "assistant", reply)
    return {"say": reply, "hangup": end}


def handle_status(session: Session, call_id: int, twilio_status: str, duration: int | None) -> None:
    call = session.get(VoiceCall, call_id)
    if call is None:
        return
    mapping = {"completed": "completed", "busy": "no_answer", "no-answer": "no_answer", "failed": "failed", "canceled": "failed"}
    new = mapping.get(twilio_status)
    if new is None:
        return
    call.status, call.ended_at, call.duration_seconds = new, utcnow(), duration
    audit.log_event(session, actor="system", actor_type=ActorType.EXTERNAL, event="call.ended", entity_type="call", entity_id=call.id,
                    data={"status": new, "duration": duration, "turns": call.turns})
    lead = session.get(Lead, call.lead_id)
    if new == "completed" and call.turns:
        _task(session, call, "Review AI call transcript and decide next step")
        crm_sync.sync_lead(session, lead, note=f"AI voice call completed: {call.turns} seller turns, {duration or 0}s.", actor="agent:voice")


approvals.register_handler(ApprovalKind.CALL, _exec_call_approval)
approvals.register_reject_hook(ApprovalKind.CALL, _reject_call)
