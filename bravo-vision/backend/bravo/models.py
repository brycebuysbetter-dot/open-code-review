from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bravo.crypto import EncryptedString
from bravo.db import Base, UTCDateTime, utcnow
from bravo.enums import ApprovalStatus, LeadStatus, MessageStatus


class _Stamped:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class User(_Stamped, Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Seller(_Stamped, Base):
    __tablename__ = "sellers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None] = mapped_column(EncryptedString)
    phone: Mapped[str | None] = mapped_column(EncryptedString)
    phone_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    email: Mapped[str | None] = mapped_column(EncryptedString)
    email_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    mailing_address: Mapped[str | None] = mapped_column(EncryptedString)
    leads: Mapped[list["Lead"]] = relationship(back_populates="seller")


class Property(_Stamped, Base):
    __tablename__ = "properties"
    id: Mapped[int] = mapped_column(primary_key=True)
    street: Mapped[str] = mapped_column(String(255))
    unit: Mapped[str | None] = mapped_column(String(32))
    city: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str | None] = mapped_column(String(2))
    zip: Mapped[str | None] = mapped_column(String(10))
    address_key: Mapped[str] = mapped_column(String(255), index=True)
    property_type: Mapped[str | None] = mapped_column(String(32))
    beds: Mapped[int | None] = mapped_column(Integer)
    baths: Mapped[float | None] = mapped_column(Numeric(4, 1))
    sqft: Mapped[int | None] = mapped_column(Integer)
    year_built: Mapped[int | None] = mapped_column(Integer)
    # Facts are only ever stored when a human or an import supplied them; never inferred.
    facts_source: Mapped[str | None] = mapped_column(String(64))
    leads: Mapped[list["Lead"]] = relationship(back_populates="property")


class Lead(_Stamped, Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    seller_id: Mapped[int | None] = mapped_column(ForeignKey("sellers.id"))
    property_id: Mapped[int | None] = mapped_column(ForeignKey("properties.id"))
    source: Mapped[str] = mapped_column(String(32), default="manual")
    status: Mapped[str] = mapped_column(String(32), default=LeadStatus.NEW, index=True)
    motivation_score: Mapped[int] = mapped_column(Integer, default=0)
    score_reasons: Mapped[list] = mapped_column(JSON, default=list)
    priority: Mapped[int] = mapped_column(Integer, default=3)  # 1 = highest
    next_action: Mapped[str | None] = mapped_column(String(255))
    next_action_due: Mapped[datetime | None] = mapped_column(UTCDateTime)
    assigned_to: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    crm_id: Mapped[str | None] = mapped_column(String(128), index=True)
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    discovery: Mapped[dict] = mapped_column(JSON, default=dict)
    raw_data: Mapped[dict] = mapped_column(JSON, default=dict)
    last_contacted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sms_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    voice_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_source: Mapped[str | None] = mapped_column(String(255))
    consent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    seller: Mapped[Seller | None] = relationship(back_populates="leads")
    property: Mapped[Property | None] = relationship(back_populates="leads")


class Conversation(_Stamped, Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    summary: Mapped[str | None] = mapped_column(EncryptedString)
    last_message_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", order_by="Message.id"
    )


class Message(_Stamped, Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    direction: Mapped[str] = mapped_column(String(8))  # inbound | outbound
    channel: Mapped[str] = mapped_column(String(16))
    subject: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(EncryptedString)
    status: Mapped[str] = mapped_column(String(24), default=MessageStatus.DRAFT, index=True)
    buyer_id: Mapped[int | None] = mapped_column(ForeignKey("buyers.id"), index=True)
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approvals.id"))
    external_id: Mapped[str | None] = mapped_column(String(128))
    scheduled_for: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(64))
    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class VoiceCall(_Stamped, Base):
    __tablename__ = "voice_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending_approval", index=True)
    goal: Mapped[str] = mapped_column(String(300), default="")
    opening: Mapped[str] = mapped_column(Text)
    twilio_sid: Mapped[str | None] = mapped_column(String(64), index=True)
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approvals.id"))
    scheduled_for: Mapped[datetime | None] = mapped_column(UTCDateTime)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    transcript: Mapped[str | None] = mapped_column(EncryptedString)  # JSON list of {role, text}
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(64))


class Task(_Stamped, Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32), default="follow_up")
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=3)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    assigned_to: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (Index("ix_tasks_dedupe", "lead_id", "kind", "status"),)


class Appointment(_Stamped, Base):
    __tablename__ = "appointments"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime)
    location: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="scheduled")
    gcal_event_id: Mapped[str | None] = mapped_column(String(128))


class DealAnalysis(_Stamped, Base):
    __tablename__ = "deal_analyses"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), index=True)
    address: Mapped[str] = mapped_column(String(255))
    inputs: Mapped[dict] = mapped_column(JSON)
    results: Mapped[dict] = mapped_column(JSON)
    assumptions: Mapped[list] = mapped_column(JSON)
    created_by: Mapped[str | None] = mapped_column(String(64))


class Offer(_Stamped, Base):
    __tablename__ = "offers"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("deal_analyses.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(24), default="draft")
    terms: Mapped[dict] = mapped_column(JSON, default=dict)
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approvals.id"))
    created_by: Mapped[str | None] = mapped_column(String(64))


class Buyer(_Stamped, Base):
    __tablename__ = "buyers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    company: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(EncryptedString)
    email_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    phone: Mapped[str | None] = mapped_column(EncryptedString)
    phone_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    sms_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    proof_of_funds_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    criteria: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str | None] = mapped_column(Text)


class BuyerInteraction(_Stamped, Base):
    __tablename__ = "buyer_interactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("buyers.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="matched")
    note: Mapped[str | None] = mapped_column(Text)
    match_reasons: Mapped[list] = mapped_column(JSON, default=list)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"))
    __table_args__ = (UniqueConstraint("lead_id", "buyer_id", name="uq_buyer_lead"),)


class Contract(_Stamped, Base):
    __tablename__ = "contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    offer_id: Mapped[int | None] = mapped_column(ForeignKey("offers.id"))
    kind: Mapped[str] = mapped_column(String(24), default="purchase")  # purchase | assignment
    status: Mapped[str] = mapped_column(String(24), default="draft")
    doc_ref: Mapped[str | None] = mapped_column(String(255))
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approvals.id"))
    created_by: Mapped[str | None] = mapped_column(String(64))


class Approval(_Stamped, Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(16), default=ApprovalStatus.PENDING, index=True)
    summary: Mapped[str] = mapped_column(String(500))
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    requested_by: Mapped[str] = mapped_column(String(64))
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    decision_note: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    result: Mapped[dict | None] = mapped_column(JSON)


class OptOut(Base):
    __tablename__ = "opt_outs"
    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(16))
    identifier_hash: Mapped[str] = mapped_column(String(64))
    identifier_mask: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(32), default="inbound_keyword")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    resubscribed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("channel", "identifier_hash", name="uq_optout"),)


class ActivityLog(Base):
    """Append-only audit trail of every decision, tool call and approval."""

    __tablename__ = "activity_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    actor_type: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (Index("ix_activity_entity", "entity_type", "entity_id"),)


@event.listens_for(ActivityLog, "before_update")
@event.listens_for(ActivityLog, "before_delete")
def _immutable(*_):
    raise RuntimeError("activity_logs is append-only")
