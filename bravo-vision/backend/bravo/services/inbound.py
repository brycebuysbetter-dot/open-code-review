from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from bravo import audit
from bravo.crypto import get_crypto
from bravo.db import utcnow
from bravo.enums import ActorType, LeadStatus, MessageStatus
from bravo.models import Buyer, BuyerInteraction, Lead, Message, Seller, Task
from bravo.services import compliance, leads, optout
from bravo.services.normalize import normalize_phone
from bravo.services.outbound import get_conversation

HELP_REPLY = "Bravo Vision automated assistant. Reply STOP to opt out. For help call the number on our website."


@dataclass
class InboundResult:
    action: str  # ignored | duplicate | stop | start | help | message
    reply: str | None = None
    lead_id: int | None = None
    message_id: int | None = None


def handle_inbound_sms(session: Session, from_raw: str, body: str, sid: str | None = None) -> InboundResult:
    phone = normalize_phone(from_raw)
    if not phone:
        return InboundResult("ignored")
    if sid and session.scalar(select(Message.id).where(Message.external_id == sid, Message.direction == "inbound")):
        return InboundResult("duplicate")  # provider webhook retry

    keyword = compliance.classify_keyword(body)
    if keyword == "stop":
        optout.record_opt_out(session, "sms", phone)
        return InboundResult("stop")  # carrier-level confirmation is sent by Twilio; do not double-text
    if keyword == "start":
        optout.resubscribe(session, "sms", phone)
        return InboundResult("start", reply="You're resubscribed. Reply STOP anytime to opt out.")
    if keyword == "help":
        return InboundResult("help", reply=HELP_REPLY)

    h = get_crypto().blind_index(phone)
    buyer = session.scalar(select(Buyer).where(Buyer.phone_hash == h))
    if buyer is not None:
        return _buyer_reply(session, buyer, body, sid)
    seller = session.scalar(select(Seller).where(Seller.phone_hash == h))
    lead = None
    if seller:
        lead = session.scalar(select(Lead).where(Lead.seller_id == seller.id, Lead.status != LeadStatus.DUPLICATE)
                              .order_by(Lead.id.desc()))
    if lead is None:
        res = leads.ingest_lead(session, {"phone": phone, "message": body}, source="sms_reply", actor="inbound_sms",
                                actor_type=ActorType.EXTERNAL)
        lead = res.lead
    if lead is None:
        return InboundResult("ignored")

    if not lead.sms_consent:  # the seller texted us first, which is consent to a reply on this thread
        leads.record_consent(session, lead, sms=True, voice=None, source="seller initiated SMS", actor="inbound_sms",
                             actor_type=ActorType.EXTERNAL)
    conv = get_conversation(session, lead.id, "sms")
    msg = Message(conversation_id=conv.id, direction="inbound", channel="sms", body=body, status=MessageStatus.RECEIVED,
                  external_id=sid, sent_at=utcnow())
    session.add(msg)
    conv.last_message_at = utcnow()
    if lead.status in (LeadStatus.NEW, LeadStatus.CONTACTED):
        lead.status = LeadStatus.ENGAGED
    session.flush()
    audit.log_event(session, actor="inbound_sms", actor_type=ActorType.EXTERNAL, event="message.received",
                    entity_type="lead", entity_id=lead.id, data={"message_id": msg.id, "channel": "sms"})
    leads.apply_scoring(session, lead)
    if not session.scalar(select(Task.id).where(Task.lead_id == lead.id, Task.kind == "reply", Task.status == "open")):
        session.add(Task(lead_id=lead.id, kind="reply", title="Seller replied: review and respond", priority=min(lead.priority, 2),
                         due_at=utcnow(), created_by="inbound_sms"))
    return InboundResult("message", lead_id=lead.id, message_id=msg.id)


def _buyer_reply(session: Session, buyer: Buyer, body: str, sid: str | None) -> InboundResult:
    inter = session.scalar(select(BuyerInteraction).where(BuyerInteraction.buyer_id == buyer.id,
                                                          BuyerInteraction.status.in_(["sent", "interested", "question"]))
                           .order_by(BuyerInteraction.id.desc()))
    if inter is None:
        return InboundResult("ignored")
    conv = get_conversation(session, inter.lead_id, "buyer_sms")
    msg = Message(conversation_id=conv.id, direction="inbound", channel="sms", body=body, status=MessageStatus.RECEIVED,
                  external_id=sid, sent_at=utcnow(), buyer_id=buyer.id)
    session.add(msg)
    session.flush()
    audit.log_event(session, actor="inbound_sms", actor_type=ActorType.EXTERNAL, event="buyer.reply_received",
                    entity_type="lead", entity_id=inter.lead_id, data={"buyer_id": buyer.id, "message_id": msg.id})
    session.add(Task(lead_id=inter.lead_id, kind="buyer_response", title=f"Buyer #{buyer.id} replied: review", priority=1,
                     due_at=utcnow(), created_by="inbound_sms", details={"buyer_id": buyer.id, "message_id": msg.id}))
    return InboundResult("buyer_message", lead_id=inter.lead_id, message_id=msg.id)
