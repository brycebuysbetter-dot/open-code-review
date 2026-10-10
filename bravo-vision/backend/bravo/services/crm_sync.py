from sqlalchemy.orm import Session

from bravo import audit
from bravo.enums import ActorType, LeadStatus
from bravo.integrations import registry
from bravo.integrations.crm.base import CRMContact, CRMDeal
from bravo.models import Lead

STAGE = {
    LeadStatus.NEW: "new", LeadStatus.CONTACTED: "contacted", LeadStatus.ENGAGED: "engaged",
    LeadStatus.APPOINTMENT_SET: "appointment_set", LeadStatus.OFFER_PENDING: "offer_pending",
    LeadStatus.OFFER_SENT: "offer_sent", LeadStatus.UNDER_CONTRACT: "under_contract",
    LeadStatus.DISPOSITION: "disposition", LeadStatus.CLOSED: "closed", LeadStatus.NURTURE: "nurture",
    LeadStatus.DEAD: "dead", LeadStatus.DO_NOT_CONTACT: "do_not_contact", LeadStatus.DUPLICATE: "duplicate",
}


def sync_lead(session: Session, lead: Lead, note: str | None = None, actor: str = "system") -> bool:
    """Push lead state (and an optional note) to the CRM. CRM outages never break the pipeline."""
    try:
        crm = registry.crm()
        seller = lead.seller
        contact_id = crm.upsert_contact(CRMContact(
            local_id=lead.id, name=seller.name if seller else None, email=seller.email if seller else None,
            phone=seller.phone if seller else None, external_id=lead.crm_id))
        lead.crm_id = contact_id
        raw = dict(lead.raw_data or {})
        title = f"{lead.property.street} {lead.property.city or ''}".strip() if lead.property else f"Lead #{lead.id}"
        deal_id = crm.upsert_deal(CRMDeal(
            local_id=lead.id, title=title, stage=STAGE.get(LeadStatus(lead.status), "new"),
            contact_external_id=contact_id, external_id=raw.get("crm_deal_id"),
            properties={}))
        raw["crm_deal_id"] = deal_id
        lead.raw_data = raw
        if note:
            crm.add_note(contact_id, note)
        audit.log_event(session, actor=actor, actor_type=ActorType.SYSTEM, event="crm.synced", entity_type="lead",
                        entity_id=lead.id, data={"provider": crm.name, "note": bool(note)})
        return True
    except Exception as exc:
        audit.log_event(session, actor=actor, actor_type=ActorType.SYSTEM, event="crm.sync_failed", entity_type="lead",
                        entity_id=lead.id, data={"error": f"{type(exc).__name__}: {exc}"})
        return False
