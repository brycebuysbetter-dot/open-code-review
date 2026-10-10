from sqlalchemy import select
from sqlalchemy.orm import Session

from bravo import audit
from bravo.crypto import get_crypto, mask_email, mask_phone
from bravo.db import utcnow
from bravo.enums import ActorType, ApprovalKind, ApprovalStatus, LeadStatus, MessageStatus
from bravo.models import Approval, Buyer, Conversation, Lead, Message, OptOut, Seller


def _mask(channel: str, identifier: str) -> str:
    return mask_phone(identifier) if channel in ("sms", "voice") else mask_email(identifier)


def _row(session: Session, channel: str, identifier: str) -> OptOut | None:
    h = get_crypto().blind_index(identifier)
    return session.scalar(select(OptOut).where(OptOut.channel == channel, OptOut.identifier_hash == h))


def is_opted_out(session: Session, channel: str, identifier: str | None) -> bool:
    if not identifier:
        return False
    row = _row(session, channel, identifier)
    return row is not None and row.resubscribed_at is None


def record_opt_out(session: Session, channel: str, identifier: str, source: str = "inbound_keyword") -> OptOut:
    row = _row(session, channel, identifier)
    if row is None:
        row = OptOut(channel=channel, identifier_hash=get_crypto().blind_index(identifier),
                     identifier_mask=_mask(channel, identifier), source=source)
        session.add(row)
    else:
        row.resubscribed_at = None
    session.flush()
    audit.log_event(session, actor="system", actor_type=ActorType.EXTERNAL, event="optout.recorded",
                    entity_type="optout", entity_id=row.id, data={"channel": channel, "id": row.identifier_mask, "source": source})
    _cascade(session, channel, identifier)
    return row


def resubscribe(session: Session, channel: str, identifier: str) -> bool:
    row = _row(session, channel, identifier)
    if row is None or row.resubscribed_at is not None:
        return False
    row.resubscribed_at = utcnow()
    audit.log_event(session, actor="system", actor_type=ActorType.EXTERNAL, event="optout.resubscribed",
                    entity_type="optout", entity_id=row.id, data={"channel": channel, "id": row.identifier_mask})
    return True


def _cascade(session: Session, channel: str, identifier: str) -> None:
    """Stop everything in flight to this person and mark their leads do-not-contact."""
    h = get_crypto().blind_index(identifier)
    bcol = Buyer.phone_hash if channel == "sms" else Buyer.email_hash
    for buyer in session.scalars(select(Buyer).where(bcol == h)):
        _block_pending(session, Message.buyer_id == buyer.id, channel)
    col = Seller.phone_hash if channel in ("sms", "voice") else Seller.email_hash
    seller_ids = list(session.scalars(select(Seller.id).where(col == h)))
    if not seller_ids:
        return
    leads = list(session.scalars(select(Lead).where(Lead.seller_id.in_(seller_ids))))
    for lead in leads:
        lead.status = LeadStatus.DO_NOT_CONTACT
        lead.next_action, lead.next_action_due, lead.priority = "No action: opted out", None, 4
    lead_ids = [lead.id for lead in leads]
    if not lead_ids:
        return
    _block_pending(session, Conversation.lead_id.in_(lead_ids) & Message.buyer_id.is_(None), channel)


def _block_pending(session: Session, condition, channel: str) -> None:
    msgs = session.scalars(
        select(Message).join(Conversation).where(
            condition, Message.direction == "outbound", Message.channel == channel,
            Message.status.in_([MessageStatus.PENDING_APPROVAL, MessageStatus.SCHEDULED, MessageStatus.DRAFT]))
    )
    for m in msgs:
        if m.approval_id:
            appr = session.get(Approval, m.approval_id)
            if appr and appr.status == ApprovalStatus.PENDING and appr.kind == ApprovalKind.MESSAGE:
                appr.status, appr.decided_by, appr.decided_at = ApprovalStatus.REJECTED, "system", utcnow()
                appr.decision_note = "recipient opted out"
        m.status, m.error = MessageStatus.BLOCKED, "recipient opted out"
