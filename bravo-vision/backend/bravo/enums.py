from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    MANAGER = "manager"
    ACQUISITIONS = "acquisitions"
    DISPOSITIONS = "dispositions"
    VIEWER = "viewer"


class LeadStatus(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    ENGAGED = "engaged"
    APPOINTMENT_SET = "appointment_set"
    OFFER_PENDING = "offer_pending"
    OFFER_SENT = "offer_sent"
    UNDER_CONTRACT = "under_contract"
    DISPOSITION = "disposition"
    CLOSED = "closed"
    NURTURE = "nurture"
    DEAD = "dead"
    DO_NOT_CONTACT = "do_not_contact"
    DUPLICATE = "duplicate"


class Channel(StrEnum):
    SMS = "sms"
    EMAIL = "email"


class MessageStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    SCHEDULED = "scheduled"
    SENT = "sent"
    FAILED = "failed"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    RECEIVED = "received"


class ApprovalKind(StrEnum):
    MESSAGE = "message"
    CAMPAIGN = "campaign"
    OFFER = "offer"
    CONTRACT = "contract"
    PAYMENT = "payment"
    DESTRUCTIVE = "destructive"
    BUYER_BLAST = "buyer_blast"
    CALL = "call"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    EXPIRED = "expired"


class ActorType(StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"
    EXTERNAL = "external"
