"""Follow-up rules, hot-lead escalation, reminders and approval-gated campaigns."""
import re
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bravo import approvals, audit
from bravo.config import get_settings
from bravo.crypto import mask_email, mask_phone
from bravo.db import utcnow
from bravo.enums import ActorType, ApprovalKind, LeadStatus, MessageStatus
from bravo.models import Appointment, Conversation, Lead, Message, Task
from bravo.services import compliance, outbound
from bravo.services.hours import next_contact_time

CONTACTED_DELAYS_DAYS = [2, 5, 10, 21]
FAMILIES = {
    "contact": {"contact", "hot_lead", "first_contact"},
    "follow_up": {"follow_up", "reply"},
    "appointment_reminder": {"appointment_reminder"},
    "offer_follow_up": {"offer_follow_up"},
    "deal_check_in": {"deal_check_in"},
    "nurture_decision": {"nurture_decision"},
}
INACTIVE = {LeadStatus.DEAD, LeadStatus.CLOSED, LeadStatus.DUPLICATE, LeadStatus.DO_NOT_CONTACT}
MAX_CAMPAIGN_SIZE = 200
CAMPAIGN_COOLDOWN_DAYS = 7
ALLOWED_PLACEHOLDERS = {"first_name", "street"}


def _activity(session: Session, lead: Lead) -> tuple[int, datetime | None, datetime | None]:
    """(sent_count, last_outbound_at, last_inbound_at) for seller threads."""
    base = select(func.count(Message.id), func.max(Message.sent_at)).join(Conversation).where(
        Conversation.lead_id == lead.id, Message.buyer_id.is_(None))
    sent, last_out = session.execute(base.where(Message.direction == "outbound", Message.status == MessageStatus.SENT)).one()
    last_in = session.scalar(select(func.max(Message.sent_at)).join(Conversation).where(
        Conversation.lead_id == lead.id, Message.buyer_id.is_(None), Message.direction == "inbound"))
    return sent or 0, last_out, last_in


def decide_followup(session: Session, lead: Lead, now: datetime) -> tuple[str, str, datetime] | None:
    sent, last_out, last_in = _activity(session, lead)
    last_touch = max([t for t in (last_out, last_in, lead.created_at) if t])
    status = LeadStatus(lead.status)
    if status == LeadStatus.NEW:
        return "contact", "Make first contact", now
    if status == LeadStatus.CONTACTED:
        if last_in and (not last_out or last_in > last_out):
            return "follow_up", "Seller replied: respond", now
        if sent > len(CONTACTED_DELAYS_DAYS):
            return "nurture_decision", "No response after multiple attempts: move to nurture or close out?", now
        delay = CONTACTED_DELAYS_DAYS[max(sent, 1) - 1]
        return "follow_up", f"Follow up (attempt {sent + 1})", (last_out or lead.created_at) + timedelta(days=delay)
    if status == LeadStatus.ENGAGED:
        return "follow_up", "Continue conversation: next discovery step", last_touch + timedelta(days=1)
    if status == LeadStatus.APPOINTMENT_SET:
        appt = session.scalar(select(Appointment).where(Appointment.lead_id == lead.id, Appointment.status == "scheduled",
                                                        Appointment.starts_at > now).order_by(Appointment.starts_at))
        if appt:
            return "appointment_reminder", "Appointment tomorrow: prep and confirm", appt.starts_at - timedelta(days=1)
        return None
    if status == LeadStatus.OFFER_SENT:
        return "offer_follow_up", "Follow up on offer", last_touch + timedelta(days=2)
    if status == LeadStatus.NURTURE:
        return "follow_up", "Monthly nurture check-in", last_touch + timedelta(days=30)
    if status in (LeadStatus.UNDER_CONTRACT, LeadStatus.DISPOSITION):
        return "deal_check_in", "Deal check-in (title, buyer, closing)", last_touch + timedelta(days=3)
    return None


def _has_open_task(session: Session, lead_id: int, kind: str) -> bool:
    family = FAMILIES.get(kind, {kind})
    return session.scalar(select(Task.id).where(Task.lead_id == lead_id, Task.status == "open", Task.kind.in_(family)).limit(1)) is not None


def scan_followups(session: Session, now: datetime | None = None, actor: str = "agent:followup") -> list[Task]:
    now = now or utcnow()
    created: list[Task] = []
    leads = session.scalars(select(Lead).where(Lead.archived.is_(False), Lead.status.not_in([s.value for s in INACTIVE]))).all()
    for lead in leads:
        plan = decide_followup(session, lead, now)
        if not plan or _has_open_task(session, lead.id, plan[0]):
            continue
        kind, title, due = plan
        state = lead.property.state if lead.property else None
        task = Task(lead_id=lead.id, kind=kind, title=title, priority=lead.priority, due_at=next_contact_time(max(due, now) if kind != "appointment_reminder" else due, state),
                    created_by=actor)
        session.add(task)
        session.flush()
        audit.log_event(session, actor=actor, actor_type=ActorType.AGENT, event="followup.task_created", entity_type="task",
                        entity_id=task.id, data={"lead_id": lead.id, "kind": kind, "due_at": task.due_at.isoformat()})
        created.append(task)
    return created


def escalate_hot_leads(session: Session, now: datetime | None = None, actor: str = "agent:followup") -> list[Task]:
    now = now or utcnow()
    threshold = get_settings().hot_lead_threshold
    out = []
    candidates = session.scalars(select(Lead).where(
        Lead.archived.is_(False), Lead.motivation_score >= threshold,
        Lead.status.in_([LeadStatus.NEW, LeadStatus.CONTACTED, LeadStatus.ENGAGED]))).all()
    for lead in candidates:
        raw = dict(lead.raw_data or {})
        last = raw.get("escalated_at")
        if last and datetime.fromisoformat(last) > now - timedelta(hours=24):
            continue
        raw["escalated_at"] = now.isoformat()
        lead.raw_data = raw
        lead.priority = 1
        task = Task(lead_id=lead.id, kind="escalation", priority=1, due_at=now, created_by=actor,
                    title=f"HOT LEAD (score {lead.motivation_score}): call now", details={"reasons": lead.score_reasons})
        session.add(task)
        session.flush()
        audit.log_event(session, actor=actor, actor_type=ActorType.AGENT, event="lead.escalated", entity_type="lead",
                        entity_id=lead.id, data={"score": lead.motivation_score, "task_id": task.id})
        out.append(task)
    return out


def due_reminders(session: Session, now: datetime | None = None) -> list[Task]:
    """Mark due tasks as reminded once; the dashboard and API surface them. Reminders go to staff, never to sellers."""
    now = now or utcnow()
    tasks = session.scalars(select(Task).where(Task.status == "open", Task.due_at <= now).order_by(Task.priority, Task.due_at)).all()
    fresh = []
    for t in tasks:
        if not (t.details or {}).get("reminded_at"):
            t.details = {**(t.details or {}), "reminded_at": now.isoformat()}
            audit.log_event(session, actor="system", event="task.reminder_due", entity_type="task", entity_id=t.id,
                            data={"lead_id": t.lead_id, "title": t.title})
            fresh.append(t)
    return fresh


# ---- campaigns --------------------------------------------------------------------------------

class CampaignError(Exception):
    pass


def validate_template(template: str, channel: str) -> None:
    unknown = set(re.findall(r"\{(\w+)\}", template)) - ALLOWED_PLACEHOLDERS
    if unknown:
        raise CampaignError(f"unknown placeholders: {sorted(unknown)}; allowed: {sorted(ALLOWED_PLACEHOLDERS)}")
    problems = compliance.check_message(re.sub(r"\{\w+\}", "x", template), channel)
    if problems:
        raise CampaignError("template fails compliance: " + "; ".join(problems))


def propose_campaign(session: Session, *, name: str, lead_ids: list[int], template: str, channel: str, requested_by: str,
                     subject: str | None = None, actor_type: ActorType = ActorType.AGENT, now: datetime | None = None):
    if channel not in ("sms", "email"):
        raise CampaignError("channel must be sms or email")
    if not lead_ids or len(lead_ids) > MAX_CAMPAIGN_SIZE:
        raise CampaignError(f"campaigns need 1-{MAX_CAMPAIGN_SIZE} leads")
    validate_template(template, channel)
    cutoff = (now or utcnow()) - timedelta(days=CAMPAIGN_COOLDOWN_DAYS)
    items, skipped = [], {}
    approval = approvals.request_approval(
        session, kind=ApprovalKind.CAMPAIGN, requested_by=requested_by, actor_type=actor_type, entity_type="campaign",
        summary=f"Campaign '{name}' ({channel})", payload={"campaign": name, "channel": channel, "items": []})
    for lid in dict.fromkeys(lead_ids):
        lead = session.get(Lead, lid)
        why = None
        if lead is None or lead.archived:
            why = "missing"
        elif lead.status in INACTIVE:
            why = "inactive_or_dnc"
        else:
            to = outbound.recipient(lead, channel)
            if not to:
                why = "no_contact"
            elif outbound.is_opted_out(session, channel, to):
                why = "opted_out"
            elif channel == "sms" and get_settings().consent_required and not lead.sms_consent:
                why = "no_consent"
            elif _activity(session, lead)[1] and _activity(session, lead)[1] > cutoff:
                why = "contacted_recently"
        if why:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        first = (lead.seller.name or "").split()[0] if lead.seller and lead.seller.name else "there"
        body = template.format(first_name=first, street=(lead.property.street.title() if lead.property else "your property"))
        conv = outbound.get_conversation(session, lead.id, channel)
        msg = Message(conversation_id=conv.id, direction="outbound", channel=channel, subject=subject,
                      body=compliance.with_footer(body, channel), status=MessageStatus.PENDING_APPROVAL,
                      created_by=requested_by, approval_id=approval.id)
        session.add(msg)
        session.flush()
        masked = mask_phone(to) if channel == "sms" else mask_email(to)
        items.append({"message_id": msg.id, "lead_id": lead.id, "to": masked, "body": msg.body})
    if not items:
        raise CampaignError(f"no eligible recipients (skipped: {skipped})")
    approval.payload = {"campaign": name, "channel": channel, "subject": subject, "items": items, "skipped": skipped}
    approval.payload_hash = approvals.payload_digest(approval.payload)
    approval.summary = f"Campaign '{name}': {len(items)} {channel} messages (skipped {sum(skipped.values())})"
    return approval
