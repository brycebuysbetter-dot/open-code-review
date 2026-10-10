"""Offers, contracts, payments and destructive operations. Each is request -> human approval -> execute."""
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from bravo import approvals
from bravo.enums import ActorType, ApprovalKind, LeadStatus
from bravo.models import (Appointment, Approval, BuyerInteraction, Contract, Conversation, DealAnalysis, Lead,
                          Message, Offer, Property, Seller, Task)
from bravo.services import outbound


class ActionError(Exception):
    pass


# ---- offers -----------------------------------------------------------------------------------

def request_offer(session: Session, *, lead_id: int, analysis_id: int, amount, requested_by: str,
                  actor_type: ActorType = ActorType.AGENT, terms: dict | None = None) -> Offer:
    analysis = session.get(DealAnalysis, analysis_id)
    lead = session.get(Lead, lead_id)
    if analysis is None or lead is None:
        raise ActionError("lead or analysis not found")
    if analysis.lead_id not in (None, lead_id):
        raise ActionError("analysis belongs to a different lead")
    try:
        amt = Decimal(str(amount))
    except Exception as exc:
        raise ActionError("amount must be a number") from exc
    mao = Decimal(analysis.results["mao"])
    if amt <= 0:
        raise ActionError("offer amount must be positive")
    if amt > mao:
        raise ActionError(f"offer {amt} exceeds the analysis MAO {mao}; run a new analysis with different inputs instead")
    offer = Offer(lead_id=lead_id, analysis_id=analysis_id, amount=amt, status="pending_approval",
                  terms=terms or {}, created_by=requested_by)
    session.add(offer)
    session.flush()
    appr = approvals.request_approval(
        session, kind=ApprovalKind.OFFER, requested_by=requested_by, actor_type=actor_type, entity_type="offer",
        entity_id=offer.id, summary=f"Offer ${amt:,.2f} for lead #{lead_id} (MAO ${mao:,.2f})",
        payload={"offer_id": offer.id, "lead_id": lead_id, "amount": str(amt), "analysis_id": analysis_id,
                 "analysis_inputs": analysis.inputs, "terms": offer.terms})
    offer.approval_id = appr.id
    return offer


def _exec_offer(session: Session, approval: Approval) -> dict:
    offer = session.get(Offer, approval.payload["offer_id"])
    offer.status = "approved"
    lead = session.get(Lead, offer.lead_id)
    lead.status = LeadStatus.OFFER_PENDING
    return {"offer_id": offer.id, "status": "approved", "note": "approved internally; nothing has been sent to the seller"}


def _reject_offer(session: Session, approval: Approval) -> dict:
    offer = session.get(Offer, approval.payload.get("offer_id", 0))
    if offer:
        offer.status = "rejected"
    return {}


def draft_offer_message(session: Session, offer_id: int, channel: str, requested_by: str,
                        actor_type: ActorType = ActorType.USER, subject: str | None = None):
    """Deterministic template: the only path where a dollar figure may appear in an outbound message."""
    offer = session.get(Offer, offer_id)
    if offer is None or offer.status != "approved":
        raise ActionError("offer must be approved before a message can be drafted")
    lead = session.get(Lead, offer.lead_id)
    where = f" for {lead.property.street}" if lead.property else ""
    body = (f"Hello, based on the information you shared, we'd like to offer ${offer.amount:,.0f} cash{where}, "
            f"purchased as-is. This is a non-binding proposal, not an appraisal or valuation, and any sale would be "
            f"subject to a written agreement. Happy to talk through it or answer questions.")
    msg = outbound.draft_message(session, lead, channel, body, subject=subject or "Our offer for your property",
                                 created_by=requested_by, allow_amounts=True, actor_type=actor_type)
    appr = session.get(Approval, msg.approval_id)
    appr.payload = {**appr.payload, "offer_id": offer.id}
    appr.payload_hash = approvals.payload_digest(appr.payload)
    return msg


# ---- contracts --------------------------------------------------------------------------------

def request_contract(session: Session, *, lead_id: int, offer_id: int | None, kind: str, doc_ref: str,
                     requested_by: str, actor_type: ActorType = ActorType.USER) -> Contract:
    """Records an attorney/title-approved document reference. This system never writes contract language."""
    if kind not in ("purchase", "assignment"):
        raise ActionError("kind must be 'purchase' or 'assignment'")
    if not doc_ref.strip():
        raise ActionError("doc_ref (link or ID of the reviewed document) is required")
    c = Contract(lead_id=lead_id, offer_id=offer_id, kind=kind, doc_ref=doc_ref.strip(), status="pending_approval",
                 created_by=requested_by)
    session.add(c)
    session.flush()
    appr = approvals.request_approval(
        session, kind=ApprovalKind.CONTRACT, requested_by=requested_by, actor_type=actor_type, entity_type="contract",
        entity_id=c.id, summary=f"{kind.title()} contract for lead #{lead_id}",
        payload={"contract_id": c.id, "lead_id": lead_id, "offer_id": offer_id, "kind": kind, "doc_ref": c.doc_ref})
    c.approval_id = appr.id
    return c


def _exec_contract(session: Session, approval: Approval) -> dict:
    c = session.get(Contract, approval.payload["contract_id"])
    c.status = "approved_to_execute"
    lead = session.get(Lead, c.lead_id)
    lead.status = LeadStatus.UNDER_CONTRACT if c.kind == "purchase" else LeadStatus.DISPOSITION
    return {"contract_id": c.id, "status": c.status, "note": "approval recorded; signing happens in your e-sign tool"}


# ---- payments ---------------------------------------------------------------------------------

def request_payment(session: Session, *, lead_id: int | None, amount, purpose: str, payee: str, requested_by: str,
                    actor_type: ActorType = ActorType.USER):
    return approvals.request_approval(
        session, kind=ApprovalKind.PAYMENT, requested_by=requested_by, actor_type=actor_type, entity_type="lead",
        entity_id=lead_id, summary=f"Payment {amount} to {payee}: {purpose}"[:500],
        payload={"lead_id": lead_id, "amount": str(amount), "payee": payee, "purpose": purpose})


def _exec_payment(session: Session, approval: Approval) -> dict:
    return {"recorded": True, "note": "Approval recorded only. This system does not move money; pay via your bank/title company."}


# ---- destructive ------------------------------------------------------------------------------

def request_destructive(session: Session, *, action: str, lead_id: int, requested_by: str,
                        actor_type: ActorType = ActorType.USER, reason: str = ""):
    if action not in ("archive_lead", "purge_lead"):
        raise ActionError("unsupported destructive action")
    if session.get(Lead, lead_id) is None:
        raise ActionError("lead not found")
    return approvals.request_approval(
        session, kind=ApprovalKind.DESTRUCTIVE, requested_by=requested_by, actor_type=actor_type, entity_type="lead",
        entity_id=lead_id, summary=f"{action.replace('_', ' ').title()} #{lead_id}: {reason}"[:500],
        payload={"action": action, "lead_id": lead_id, "reason": reason})


def _exec_destructive(session: Session, approval: Approval) -> dict:
    lead = session.get(Lead, approval.payload["lead_id"])
    if lead is None:
        return {"error": "lead no longer exists"}
    if approval.payload["action"] == "archive_lead":
        lead.archived = True
        return {"archived": lead.id}
    return _purge(session, lead)


def _purge(session: Session, lead: Lead) -> dict:
    lid, seller_id, prop_id = lead.id, lead.seller_id, lead.property_id
    conv_ids = list(session.scalars(select(Conversation.id).where(Conversation.lead_id == lid)))
    if conv_ids:
        session.execute(delete(BuyerInteraction).where(BuyerInteraction.message_id.in_(select(Message.id).where(Message.conversation_id.in_(conv_ids)))))
        session.execute(delete(Message).where(Message.conversation_id.in_(conv_ids)))
        session.execute(delete(Conversation).where(Conversation.id.in_(conv_ids)))
    for model in (BuyerInteraction, Task, Appointment, Contract, Offer, DealAnalysis):
        session.execute(delete(model).where(model.lead_id == lid))
    session.execute(delete(Lead).where(Lead.duplicate_of_id == lid))
    session.delete(lead)
    session.flush()
    if seller_id and not session.scalar(select(Lead.id).where(Lead.seller_id == seller_id)):
        session.execute(delete(Seller).where(Seller.id == seller_id))
    if prop_id and not session.scalar(select(Lead.id).where(Lead.property_id == prop_id)):
        session.execute(delete(Property).where(Property.id == prop_id))
    return {"purged_lead": lid}


approvals.register_handler(ApprovalKind.OFFER, _exec_offer)
approvals.register_reject_hook(ApprovalKind.OFFER, _reject_offer)
approvals.register_handler(ApprovalKind.CONTRACT, _exec_contract)
approvals.register_handler(ApprovalKind.PAYMENT, _exec_payment)
approvals.register_handler(ApprovalKind.DESTRUCTIVE, _exec_destructive)
