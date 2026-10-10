"""Every outbound message passes through here: draft -> human approval -> gated send."""
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bravo import approvals, audit
from bravo.config import get_settings
from bravo.crypto import mask_email, mask_phone
from bravo.db import utcnow
from bravo.enums import ActorType, ApprovalKind, LeadStatus, MessageStatus
from bravo.integrations import registry
from bravo.integrations.base import PermanentSendError
from bravo.models import Approval, Buyer, Conversation, Lead, Message
from bravo.retry import TransientError
from bravo.services import compliance
from bravo.services.hours import contact_tz, is_contact_allowed, next_contact_time
from bravo.services.optout import is_opted_out


class ComplianceError(Exception):
    def __init__(self, violations: list[str]):
        super().__init__("; ".join(violations))
        self.violations = violations


def recipient(lead: Lead, channel: str) -> str | None:
    if lead.seller is None:
        return None
    return lead.seller.phone if channel == "sms" else lead.seller.email


def get_conversation(session: Session, lead_id: int, channel: str) -> Conversation:
    conv = session.scalar(select(Conversation).where(Conversation.lead_id == lead_id, Conversation.channel == channel))
    if conv is None:
        conv = Conversation(lead_id=lead_id, channel=channel)
        session.add(conv)
        session.flush()
    return conv


def draft_message(
    session: Session, lead: Lead, channel: str, body: str, *, subject: str | None = None, created_by: str,
    allow_amounts: bool = False, actor_type: ActorType = ActorType.AGENT, kind: ApprovalKind = ApprovalKind.MESSAGE,
) -> Message:
    to = recipient(lead, channel)
    violations = compliance.check_message(body, channel, allow_amounts=allow_amounts)
    if not to:
        violations.append(f"lead has no {channel} contact on file")
    elif is_opted_out(session, channel, to):
        violations.append("recipient has opted out")
    elif channel == "sms" and get_settings().consent_required and not lead.sms_consent:
        violations.append("no recorded SMS consent for this lead (POST /leads/{id}/consent)")
    if lead.status == LeadStatus.DO_NOT_CONTACT:
        violations.append("lead is marked do-not-contact")
    if violations:
        audit.log_event(session, actor=created_by, actor_type=actor_type, event="message.draft_blocked",
                        entity_type="lead", entity_id=lead.id, data={"violations": violations, "channel": channel})
        raise ComplianceError(violations)

    conv = get_conversation(session, lead.id, channel)
    final_body = compliance.with_footer(body, channel)
    msg = Message(conversation_id=conv.id, direction="outbound", channel=channel, subject=subject, body=final_body,
                  status=MessageStatus.PENDING_APPROVAL, created_by=created_by)
    session.add(msg)
    session.flush()
    masked = mask_phone(to) if channel == "sms" else mask_email(to)
    approval = approvals.request_approval(
        session, kind=kind, requested_by=created_by, actor_type=actor_type, entity_type="message", entity_id=msg.id,
        summary=f"{channel.upper()} to {masked} (lead #{lead.id})",
        payload={"message_id": msg.id, "lead_id": lead.id, "channel": channel, "to": masked, "subject": subject, "body": final_body},
    )
    msg.approval_id = approval.id
    return msg


def _start_of_local_day(now: datetime, state: str | None) -> datetime:
    tz = contact_tz(state)
    return now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(now.tzinfo)


def _sent_count(session: Session, since: datetime, lead_id: int | None = None) -> int:
    q = select(func.count(Message.id)).join(Conversation).where(
        Message.direction == "outbound", Message.channel == "sms", Message.status == MessageStatus.SENT, Message.sent_at >= since)
    if lead_id is not None:
        q = q.where(Conversation.lead_id == lead_id, Message.buyer_id.is_(None))
    return session.scalar(q) or 0


def evaluate(session: Session, lead: Lead, channel: str, now: datetime, buyer: Buyer | None = None) -> tuple[str, datetime | None, str | None]:
    """Return (decision, defer_until, reason). decision in send | defer | block."""
    s = get_settings()
    if buyer is not None:
        to = buyer.phone if channel == "sms" else buyer.email
        if not buyer.active or not buyer.approved:
            return "block", None, "buyer is not an active approved buyer"
    else:
        to = recipient(lead, channel)
    if not to:
        return "block", None, f"no {channel} contact on file"
    if (buyer is None and lead.status == LeadStatus.DO_NOT_CONTACT) or is_opted_out(session, channel, to):
        return "block", None, "recipient opted out"
    if channel == "sms" and s.consent_required and not (buyer.sms_consent if buyer is not None else lead.sms_consent):
        return "block", None, "no recorded SMS consent"
    state = lead.property.state if (lead.property and buyer is None) else None
    if not is_contact_allowed(now, state):
        return "defer", next_contact_time(now, state), "outside approved contact hours"
    if channel == "sms":
        day_start = _start_of_local_day(now, state)
        if _sent_count(session, day_start) >= s.max_sms_per_day or _sent_count(session, day_start, lead.id) >= s.max_sms_per_lead_per_day:
            tomorrow = (day_start + timedelta(days=1, hours=1))
            return "defer", next_contact_time(tomorrow, state), "daily send cap reached"
    return "send", None, None


def attempt_send(session: Session, msg: Message, now: datetime | None = None) -> dict:
    now = now or utcnow()
    lead = session.get(Lead, msg.conversation.lead_id)
    buyer = session.get(Buyer, msg.buyer_id) if msg.buyer_id else None
    decision, defer_until, reason = evaluate(session, lead, msg.channel, now, buyer)
    if decision == "block":
        msg.status, msg.error = MessageStatus.BLOCKED, reason
        audit.log_event(session, actor="system", event="message.blocked", entity_type="message", entity_id=msg.id, data={"reason": reason})
        return {"status": "blocked", "error": reason}
    if decision == "defer":
        msg.status, msg.scheduled_for = MessageStatus.SCHEDULED, defer_until
        audit.log_event(session, actor="system", event="message.scheduled", entity_type="message", entity_id=msg.id,
                        data={"reason": reason, "scheduled_for": defer_until.isoformat()})
        return {"status": "scheduled", "scheduled_for": defer_until.isoformat(), "reason": reason}
    if not get_settings().automatic_messaging_enabled:
        hold_until = now + timedelta(minutes=5)
        msg.status, msg.scheduled_for = MessageStatus.SCHEDULED, hold_until
        audit.log_event(session, actor="system", event="message.held", entity_type="message", entity_id=msg.id,
                        data={"reason": "AUTOMATIC_MESSAGING_ENABLED is false"})
        return {"status": "scheduled", "scheduled_for": hold_until.isoformat(), "reason": "automatic messaging is disabled"}
    to = (buyer.phone if msg.channel == "sms" else buyer.email) if buyer else recipient(lead, msg.channel)
    try:
        if msg.channel == "sms":
            res = registry.sms().send(to, msg.body)
        else:
            res = registry.email().send(to, msg.subject or "Following up on your property", msg.body)
    except (PermanentSendError, TransientError, RuntimeError) as exc:
        msg.status, msg.error = MessageStatus.FAILED, str(exc)[:500]
        audit.log_event(session, actor="system", event="message.send_failed", entity_type="message", entity_id=msg.id, data={"error": str(exc)})
        return {"status": "failed", "error": str(exc)[:500]}
    msg.status, msg.sent_at, msg.external_id, msg.error = MessageStatus.SENT, now, res.external_id, None
    msg.scheduled_for = None
    if buyer is None:
        lead.last_contacted_at = now
        if lead.status == LeadStatus.NEW:
            lead.status = LeadStatus.CONTACTED
    conv = msg.conversation
    conv.last_message_at = now
    audit.log_event(session, actor="system", event="message.sent", entity_type="message", entity_id=msg.id,
                    data={"channel": msg.channel, "external_id": res.external_id, "lead_id": lead.id})
    return {"status": "sent", "external_id": res.external_id}


def _execute_message_approval(session: Session, approval: Approval) -> dict:
    msg = session.get(Message, approval.payload["message_id"])
    if msg is None or msg.body != approval.payload["body"]:
        return {"error": "message content differs from what was approved"}
    if msg.status != MessageStatus.PENDING_APPROVAL:
        return {"error": f"message is {msg.status}, not pending approval"}
    result = attempt_send(session, msg)
    offer_id = approval.payload.get("offer_id")
    if offer_id and result.get("status") == "sent":
        from bravo.models import Offer

        offer = session.get(Offer, offer_id)
        if offer:
            offer.status = "sent"
            session.get(Lead, offer.lead_id).status = LeadStatus.OFFER_SENT
    return result


def _on_reject(session: Session, approval: Approval) -> dict:
    msg = session.get(Message, approval.payload.get("message_id", 0))
    if msg and msg.status == MessageStatus.PENDING_APPROVAL:
        msg.status = MessageStatus.REJECTED
    return {}


def dispatch_due(session: Session, now: datetime | None = None, limit: int = 50) -> list[dict]:
    """Send approved messages that were deferred for contact hours or caps. Safe to call from cron."""
    now = now or utcnow()
    due = session.scalars(
        select(Message).where(Message.status == MessageStatus.SCHEDULED, Message.scheduled_for <= now)
        .order_by(Message.scheduled_for).limit(limit)
    ).all()
    results = []
    for m in due:
        results.append({"message_id": m.id, **attempt_send(session, m, now)})
    return results


def execute_batch(session: Session, approval: Approval) -> dict:
    """Campaign / buyer-blast approvals: one human decision covers the exact list of messages shown."""
    counts: dict[str, int] = {}
    for item in approval.payload["items"]:
        msg = session.get(Message, item["message_id"])
        if msg is None or msg.body != item["body"] or msg.status != MessageStatus.PENDING_APPROVAL:
            outcome = "skipped"
        else:
            outcome = attempt_send(session, msg)["status"]
            if msg.buyer_id:
                from bravo.models import BuyerInteraction

                inter = session.scalar(select(BuyerInteraction).where(
                    BuyerInteraction.lead_id == item["lead_id"], BuyerInteraction.buyer_id == msg.buyer_id))
                if inter and outcome in ("sent", "scheduled"):
                    inter.status, inter.message_id = "sent" if outcome == "sent" else "scheduled", msg.id
        counts[outcome] = counts.get(outcome, 0) + 1
    result: dict = {"counts": counts}
    if counts.get("sent", 0) + counts.get("scheduled", 0) == 0:
        result["error"] = "no messages were sent or scheduled"
    return result


def reject_batch(session: Session, approval: Approval) -> dict:
    for item in approval.payload["items"]:
        msg = session.get(Message, item["message_id"])
        if msg and msg.status == MessageStatus.PENDING_APPROVAL:
            msg.status = MessageStatus.REJECTED
    from bravo.models import BuyerInteraction

    for item in approval.payload["items"]:
        if item.get("buyer_id"):
            inter = session.scalar(select(BuyerInteraction).where(
                BuyerInteraction.lead_id == item["lead_id"], BuyerInteraction.buyer_id == item["buyer_id"]))
            if inter and inter.status == "drafted":
                inter.status = "cancelled"
    return {}


approvals.register_handler(ApprovalKind.MESSAGE, _execute_message_approval)
approvals.register_handler(ApprovalKind.CAMPAIGN, execute_batch)
approvals.register_handler(ApprovalKind.BUYER_BLAST, execute_batch)
approvals.register_reject_hook(ApprovalKind.MESSAGE, _on_reject)
approvals.register_reject_hook(ApprovalKind.CAMPAIGN, reject_batch)
approvals.register_reject_hook(ApprovalKind.BUYER_BLAST, reject_batch)
