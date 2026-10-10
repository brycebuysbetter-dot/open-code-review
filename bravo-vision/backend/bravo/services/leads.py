from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from bravo import audit
from bravo.crypto import get_crypto
from bravo.db import utcnow
from bravo.enums import ActorType, LeadStatus
from bravo.models import Conversation, Lead, Message, Property, Seller, Task
from bravo.services import scoring
from bravo.services.hours import next_contact_time
from bravo.services.normalize import normalize_address, normalize_email, normalize_phone
from bravo.services.optout import is_opted_out

DISCOVERY_KEYS = ("timeline", "condition", "mortgage", "occupancy", "price_expectation", "reason")
OPEN_STATUSES = {s for s in LeadStatus} - {LeadStatus.DUPLICATE, LeadStatus.DEAD, LeadStatus.CLOSED}


@dataclass
class IngestResult:
    lead: Lead | None
    created: bool = False
    duplicate_of: Lead | None = None
    issues: list[str] = field(default_factory=list)


def _blind(value: str | None) -> str | None:
    return get_crypto().blind_index(value) if value else None


def find_duplicate(session: Session, address_key: str | None, phone_hash: str | None, email_hash: str | None) -> Lead | None:
    """Same property = duplicate. With no address, same seller contact on an address-less lead = duplicate."""
    open_ = (Lead.archived.is_(False), Lead.status != LeadStatus.DUPLICATE)
    if address_key:
        found = session.scalar(
            select(Lead).join(Property).where(Property.address_key == address_key, *open_).order_by(Lead.id)
        )
        if found:
            return found
        return None
    conds = [c for c in (Seller.phone_hash == phone_hash if phone_hash else None,
                         Seller.email_hash == email_hash if email_hash else None) if c is not None]
    if not conds:
        return None
    from sqlalchemy import or_

    return session.scalar(
        select(Lead).join(Seller).where(or_(*conds), Lead.property_id.is_(None), *open_).order_by(Lead.id)
    )


def lead_text(session: Session, lead: Lead, extra: str = "") -> str:
    """Everything the seller has told us in free text, so re-scoring never forgets earlier signals."""
    raw = lead.raw_data or {}
    parts = [str(raw.get("notes") or ""), str(raw.get("message") or "")]
    parts += [d.get("notes", "") for d in raw.get("duplicate_submissions", [])]
    if lead.id is not None:
        parts += list(session.scalars(
            select(Message.body).join(Conversation).where(Conversation.lead_id == lead.id, Message.direction == "inbound", Message.buyer_id.is_(None))))
    return " ".join(p for p in parts + [extra] if p)


def apply_scoring(session: Session, lead: Lead, text: str = "", now=None) -> None:
    now = now or utcnow()
    result = scoring.score_motivation(lead.source, lead.discovery, lead_text(session, lead, text))
    lead.motivation_score, lead.score_reasons = result.score, result.reasons
    if lead.status in (LeadStatus.DO_NOT_CONTACT, LeadStatus.DEAD, LeadStatus.DUPLICATE):
        lead.next_action, lead.priority, lead.next_action_due = "No action", 4, None
        return
    action, priority, due_hint = scoring.next_action_for(result.score, lead.status, lead.source)
    lead.next_action, lead.priority = action, priority
    state = lead.property.state if lead.property else None
    base = {"immediate": now, "same_day": now, "next_day": now + timedelta(days=1), "week": now + timedelta(days=7)}[due_hint]
    lead.next_action_due = next_contact_time(base, state)


def ingest_lead(session: Session, record: dict, *, source: str, actor: str = "system",
                actor_type: ActorType = ActorType.SYSTEM) -> IngestResult:
    phone = normalize_phone(record.get("phone"))
    email = normalize_email(record.get("email"))
    addr = normalize_address(
        street=record.get("street"), city=record.get("city"), state=record.get("state"),
        zip=record.get("zip"), one_line=record.get("address"),
    )
    if not (phone or email or addr):
        audit.log_event(session, actor=actor, actor_type=actor_type, event="lead.rejected",
                        data={"source": source, "reason": "no usable phone, email, or address"})
        return IngestResult(None, issues=["no usable phone, email, or address"])
    issues = []
    if record.get("phone") and not phone:
        issues.append("phone could not be validated")
    if not addr:
        issues.append("missing property address")

    ph, eh = _blind(phone), _blind(email)
    dup = find_duplicate(session, addr["address_key"] if addr else None, ph, eh)
    if dup:
        _merge_into(session, dup, record, phone, email, ph, eh, source)
        audit.log_event(session, actor=actor, actor_type=actor_type, event="lead.duplicate_detected",
                        entity_type="lead", entity_id=dup.id, data={"source": source})
        return IngestResult(dup, created=False, duplicate_of=dup, issues=issues)

    seller = None
    if ph or eh:
        for col, h in ((Seller.phone_hash, ph), (Seller.email_hash, eh)):
            if h and (seller := session.scalar(select(Seller).where(col == h))):
                break
    if seller is None:
        seller = Seller(name=record.get("name"), phone=phone, phone_hash=ph, email=email, email_hash=eh)
        session.add(seller)
    else:
        _fill_seller(seller, record, phone, email, ph, eh)

    prop = None
    if addr:
        prop = session.scalar(select(Property).where(Property.address_key == addr["address_key"]))
        if prop is None:
            prop = Property(street=addr["street"], unit=addr["unit"], city=addr["city"], state=addr["state"],
                            zip=addr["zip"], address_key=addr["address_key"])
            session.add(prop)
    discovery = {k: record[k] for k in DISCOVERY_KEYS if record.get(k)}
    lead = Lead(seller=seller, property=prop, source=source, discovery=discovery,
                **consent_fields(record, source),
                crm_id=record.get("crm_id"), raw_data={k: v for k, v in record.items() if k not in ("phone", "email", "name")})
    session.add(lead)
    session.flush()
    if phone and is_opted_out(session, "sms", phone):
        lead.status = LeadStatus.DO_NOT_CONTACT
    apply_scoring(session, lead)
    audit.log_event(session, actor=actor, actor_type=actor_type, event="lead.ingested", entity_type="lead", entity_id=lead.id,
                    data={"source": source, "score": lead.motivation_score, "priority": lead.priority,
                          "next_action": lead.next_action, "reasons": lead.score_reasons, "issues": issues})
    if lead.status == LeadStatus.NEW and lead.priority <= 2:
        _ensure_task(session, lead, actor)
    return IngestResult(lead, created=True, issues=issues)


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "yes", "y", "1", "on")


def consent_fields(record: dict, source: str) -> dict:
    """Consent is only ever recorded from an explicit signal in the record, never assumed."""
    sms, voice = _truthy(record.get("sms_consent")), _truthy(record.get("voice_consent"))
    if not (sms or voice):
        return {}
    return {"sms_consent": sms, "voice_consent": voice, "consent_at": utcnow(),
            "consent_source": str(record.get("consent_source") or f"{source} submission")[:255]}


def record_consent(session: Session, lead: Lead, *, sms: bool | None, voice: bool | None, source: str, actor: str,
                   actor_type: ActorType = ActorType.USER) -> None:
    before = (lead.sms_consent, lead.voice_consent)
    if sms is not None:
        lead.sms_consent = sms
    if voice is not None:
        lead.voice_consent = voice
    lead.consent_source, lead.consent_at = source[:255], utcnow()
    audit.log_event(session, actor=actor, actor_type=actor_type, event="lead.consent_recorded", entity_type="lead", entity_id=lead.id,
                    data={"sms_before": before[0], "voice_before": before[1], "sms": lead.sms_consent, "voice": lead.voice_consent,
                          "source": source[:255]})


def _fill_seller(seller: Seller, record, phone, email, ph, eh) -> None:
    if not seller.name and record.get("name"):
        seller.name = record["name"]
    if not seller.phone and phone:
        seller.phone, seller.phone_hash = phone, ph
    if not seller.email and email:
        seller.email, seller.email_hash = email, eh


def _merge_into(session: Session, lead: Lead, record, phone, email, ph, eh, source) -> None:
    if lead.seller is None:
        lead.seller = Seller()
        session.add(lead.seller)
    _fill_seller(lead.seller, record, phone, email, ph, eh)
    raw = dict(lead.raw_data or {})
    raw.setdefault("duplicate_submissions", []).append(
        {"source": source, "at": utcnow().isoformat(), "notes": str(record.get("notes") or record.get("message") or "")[:500]}
    )
    lead.raw_data = raw
    for k, v in consent_fields(record, source).items():
        if k in ("sms_consent", "voice_consent") and v and not getattr(lead, k):
            setattr(lead, k, True)
            lead.consent_source, lead.consent_at = consent_fields(record, source)["consent_source"], utcnow()
    for k in DISCOVERY_KEYS:
        if record.get(k) and not (lead.discovery or {}).get(k):
            lead.discovery = {**(lead.discovery or {}), k: record[k]}
    apply_scoring(session, lead)


def _ensure_task(session: Session, lead: Lead, actor: str) -> Task | None:
    kind = "hot_lead" if lead.priority == 1 else "contact"
    exists = session.scalar(select(Task).where(Task.lead_id == lead.id, Task.kind == kind, Task.status == "open"))
    if exists:
        return None
    task = Task(lead_id=lead.id, kind=kind, title=lead.next_action or "Contact seller", priority=lead.priority,
                due_at=lead.next_action_due, created_by=actor)
    session.add(task)
    return task
