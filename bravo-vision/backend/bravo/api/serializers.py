from bravo.crypto import mask_email, mask_phone
from bravo.enums import Role
from bravo.models import Approval, Buyer, Lead, Message, Task


def lead_out(lead: Lead, role: str) -> dict:
    s, p = lead.seller, lead.property
    masked = role == Role.VIEWER
    seller = None
    if s:
        seller = {
            "name": (s.name or "").split(" ")[0] if masked and s.name else s.name,
            "phone": mask_phone(s.phone) if masked else s.phone,
            "email": mask_email(s.email) if masked else s.email,
        }
    return {
        "id": lead.id, "status": lead.status, "source": lead.source, "motivation_score": lead.motivation_score,
        "score_reasons": lead.score_reasons, "priority": lead.priority, "next_action": lead.next_action,
        "next_action_due": lead.next_action_due, "discovery": lead.discovery, "seller": seller,
        "property": ({"street": p.street, "unit": p.unit, "city": p.city, "state": p.state, "zip": p.zip, "type": p.property_type,
                      "beds": p.beds, "baths": float(p.baths) if p.baths is not None else None, "sqft": p.sqft} if p else None),
        "sms_consent": lead.sms_consent, "voice_consent": lead.voice_consent, "consent_source": lead.consent_source,
        "last_contacted_at": lead.last_contacted_at, "created_at": lead.created_at, "archived": lead.archived,
    }


def message_out(m: Message) -> dict:
    return {"id": m.id, "direction": m.direction, "channel": m.channel, "subject": m.subject, "body": m.body, "status": m.status,
            "approval_id": m.approval_id, "scheduled_for": m.scheduled_for, "sent_at": m.sent_at, "error": m.error,
            "buyer_id": m.buyer_id, "created_by": m.created_by, "created_at": m.created_at}


def approval_out(a: Approval) -> dict:
    return {"id": a.id, "kind": a.kind, "status": a.status, "summary": a.summary, "payload": a.payload,
            "requested_by": a.requested_by, "decided_by": a.decided_by, "decided_at": a.decided_at,
            "decision_note": a.decision_note, "result": a.result, "expires_at": a.expires_at, "created_at": a.created_at,
            "entity_type": a.entity_type, "entity_id": a.entity_id}


def task_out(t: Task) -> dict:
    return {"id": t.id, "lead_id": t.lead_id, "title": t.title, "kind": t.kind, "status": t.status, "priority": t.priority,
            "due_at": t.due_at, "created_by": t.created_by}


def buyer_out(b: Buyer, role: str = "") -> dict:
    masked = role == Role.VIEWER
    return {"id": b.id, "name": b.name, "company": b.company, "email": mask_email(b.email) if masked else b.email,
            "phone": mask_phone(b.phone) if masked else b.phone, "approved": b.approved,
            "proof_of_funds_verified": b.proof_of_funds_verified, "sms_consent": b.sms_consent, "active": b.active, "criteria": b.criteria, "notes": b.notes}
