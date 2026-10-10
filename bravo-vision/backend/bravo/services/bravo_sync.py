"""Inbound side of the Bravo Vision sync: idempotent lead upserts and a PII-free status feed."""
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from bravo import audit
from bravo.enums import ActorType
from bravo.models import Lead
from bravo.services import leads

MAX_BATCH = 200


def upsert_lead(session: Session, item: dict) -> dict:
    ext = str(item.get("external_id") or "").strip()
    if not ext:
        return {"external_id": None, "error": "external_id is required"}
    record = {k: v for k, v in item.items() if k not in ("external_id", "status", "id")}
    record["crm_id"] = ext
    existing = session.scalar(select(Lead).where(Lead.crm_id == ext, Lead.archived.is_(False)))
    if existing is not None:
        # Idempotent re-push: merge new facts, never create a second lead for the same Bravo Vision ID.
        leads._merge_into(session, existing, record, *_contact(record), "bravo_vision")
        res = leads.IngestResult(existing, created=False, duplicate_of=existing)
    else:
        res = leads.ingest_lead(session, record, source="bravo_vision", actor="bravo_sync", actor_type=ActorType.EXTERNAL)
        if res.lead is not None and not res.lead.crm_id:
            res.lead.crm_id = ext
    if res.lead is None:
        return {"external_id": ext, "error": "; ".join(res.issues)}
    audit.log_event(session, actor="bravo_sync", actor_type=ActorType.EXTERNAL, event="sync.bravo_lead_upserted", entity_type="lead",
                    entity_id=res.lead.id, data={"created": res.created, "external_id": ext})
    return {"external_id": ext, "lead_id": res.lead.id, "created": res.created,
            "duplicate_of_existing": bool(res.duplicate_of and not res.created)}


def _contact(record: dict):
    from bravo.crypto import get_crypto
    from bravo.services.normalize import normalize_email, normalize_phone

    phone, email = normalize_phone(record.get("phone")), normalize_email(record.get("email"))
    c = get_crypto()
    return phone, email, (c.blind_index(phone) if phone else None), (c.blind_index(email) if email else None)


def export_since(session: Session, since: datetime | None, limit: int = 500) -> list[dict]:
    stmt = select(Lead).where(Lead.crm_id.is_not(None)).order_by(Lead.updated_at).limit(limit)
    if since is not None:
        since = since if since.tzinfo else since.replace(tzinfo=UTC)
        stmt = stmt.where(Lead.updated_at > since)
    return [{"external_id": lead.crm_id, "jarvis_lead_id": lead.id, "status": lead.status, "motivation_score": lead.motivation_score,
             "priority": lead.priority, "next_action": lead.next_action, "next_action_due": lead.next_action_due,
             "discovery": lead.discovery, "sms_consent": lead.sms_consent, "voice_consent": lead.voice_consent,
             "last_contacted_at": lead.last_contacted_at, "updated_at": lead.updated_at} for lead in session.scalars(stmt)]
