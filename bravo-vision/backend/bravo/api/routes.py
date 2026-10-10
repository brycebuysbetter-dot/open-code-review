from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from bravo import approvals, audit
from bravo.agents import intake as intake_agents
from bravo.agents.deal import DealAnalysisAgent, render_summary, save_analysis
from bravo.agents.disposition import BuyerDispositionAgent
from bravo.agents.followup import FollowUpAgent
from bravo.agents.llm import LLMUnavailable
from bravo.agents.seller import SellerConversationAgent
from bravo.api import deps
from bravo.api.serializers import approval_out, buyer_out, lead_out, message_out, task_out
from bravo.config import get_settings
from bravo.db import get_session, session_scope
from bravo.enums import ActorType, ApprovalStatus, LeadStatus, MessageStatus, Role
from bravo.integrations.twilio_sms import valid_twilio_signature
from bravo.models import (ActivityLog, Appointment, Approval, Buyer, Conversation, DealAnalysis, Lead, Message, Task, User, VoiceCall)
from bravo.services import actions, bravo_sync, buyers, crm_sync, deal_math, followup, inbound, leads, outbound, voice

router = APIRouter(prefix="/api")


def _lead_or_404(session: Session, lead_id: int) -> Lead:
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, "lead not found")
    return lead


def _llm_guard(fn):
    try:
        return fn()
    except LLMUnavailable as exc:
        raise HTTPException(503, f"AI model unavailable: {exc}") from exc


# ---- health / dashboard -----------------------------------------------------------------------

@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/dashboard")
def dashboard(user: User = Depends(deps.require("leads:read")), session: Session = Depends(get_session)):
    count = lambda q: session.scalar(q) or 0  # noqa: E731
    return {
        "pending_approvals": count(select(func.count(Approval.id)).where(Approval.status == ApprovalStatus.PENDING)),
        "hot_leads": count(select(func.count(Lead.id)).where(Lead.motivation_score >= get_settings().hot_lead_threshold,
                                                              Lead.archived.is_(False), Lead.status.not_in(["dead", "closed", "duplicate", "do_not_contact"]))),
        "open_tasks": count(select(func.count(Task.id)).where(Task.status == "open")),
        "scheduled_messages": count(select(func.count(Message.id)).where(Message.status == MessageStatus.SCHEDULED)),
        "total_leads": count(select(func.count(Lead.id)).where(Lead.archived.is_(False))),
    }


# ---- users ------------------------------------------------------------------------------------

class UserIn(BaseModel):
    email: str
    name: str = ""
    role: Role


@router.post("/users")
def create_user(body: UserIn, admin: User = Depends(deps.require("users:manage")), session: Session = Depends(get_session)):
    token = deps.new_token()
    if session.scalar(select(User.id).where(User.email == body.email.lower())):
        raise HTTPException(409, "email already exists")
    u = User(email=body.email.lower(), name=body.name, role=body.role.value, token_hash=deps.hash_token(token))
    session.add(u)
    session.flush()
    audit.log_event(session, actor=deps.label(admin), actor_type=ActorType.USER, event="user.created", entity_type="user",
                    entity_id=u.id, data={"role": u.role})
    return {"id": u.id, "email": u.email, "role": u.role, "token": token, "note": "store this token now; it cannot be shown again"}


@router.get("/users")
def list_users(admin: User = Depends(deps.require("users:manage")), session: Session = Depends(get_session)):
    return [{"id": u.id, "email": u.email, "name": u.name, "role": u.role, "active": u.active} for u in session.scalars(select(User))]


@router.post("/users/{user_id}/deactivate")
def deactivate_user(user_id: int, admin: User = Depends(deps.require("users:manage")), session: Session = Depends(get_session)):
    u = session.get(User, user_id)
    if u is None:
        raise HTTPException(404, "user not found")
    if u.id == admin.id:
        raise HTTPException(400, "cannot deactivate yourself")
    u.active = False
    audit.log_event(session, actor=deps.label(admin), actor_type=ActorType.USER, event="user.deactivated", entity_type="user", entity_id=u.id)
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(deps.current_user)):
    from bravo.rbac import ROLE_PERMISSIONS

    return {"id": user.id, "email": user.email, "role": user.role, "owner_name": get_settings().owner_name, "timezone": get_settings().business_tz, "permissions": sorted(ROLE_PERMISSIONS.get(user.role, []))}


# ---- leads ------------------------------------------------------------------------------------

class LeadIn(BaseModel):
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    street: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    address: str | None = None
    notes: str | None = Field(default=None, max_length=5000)
    timeline: str | None = None
    condition: str | None = None
    mortgage: str | None = None
    occupancy: str | None = None
    price_expectation: str | None = None
    reason: str | None = None


@router.get("/leads")
def list_leads(status: str | None = None, min_score: int = 0, q: str | None = None, limit: int = Query(100, le=500),
               user: User = Depends(deps.require("leads:read")), session: Session = Depends(get_session)):
    from bravo.models import Property

    stmt = select(Lead).outerjoin(Property).where(Lead.archived.is_(False), Lead.motivation_score >= min_score)
    if status:
        stmt = stmt.where(Lead.status == status)
    if q:
        stmt = stmt.where(or_(Property.street.ilike(f"%{q}%"), Property.city.ilike(f"%{q}%"), Property.zip == q))
    rows = session.scalars(stmt.order_by(Lead.priority, Lead.motivation_score.desc(), Lead.id.desc()).limit(limit)).all()
    return [lead_out(r, user.role) for r in rows]


@router.post("/leads")
def create_lead(body: LeadIn, user: User = Depends(deps.require("leads:write")), session: Session = Depends(get_session)):
    res = leads.ingest_lead(session, body.model_dump(exclude_none=True), source="manual", actor=deps.label(user), actor_type=ActorType.USER)
    if res.lead is None:
        raise HTTPException(422, "; ".join(res.issues))
    return {"lead": lead_out(res.lead, user.role), "created": res.created, "duplicate_of": res.duplicate_of.id if res.duplicate_of else None,
            "issues": res.issues}


@router.get("/leads/{lead_id}")
def get_lead(lead_id: int, user: User = Depends(deps.require("leads:read")), session: Session = Depends(get_session)):
    lead = _lead_or_404(session, lead_id)
    out = lead_out(lead, user.role)
    out["summaries"] = [{"channel": c.channel, "summary": c.summary}
                        for c in session.scalars(select(Conversation).where(Conversation.lead_id == lead_id)) if c.summary]
    return out


class LeadPatch(BaseModel):
    status: LeadStatus | None = None
    assigned_to: int | None = None


@router.patch("/leads/{lead_id}")
def patch_lead(lead_id: int, body: LeadPatch, user: User = Depends(deps.require("leads:write")), session: Session = Depends(get_session)):
    lead = _lead_or_404(session, lead_id)
    if body.status:
        old = lead.status
        lead.status = body.status.value
        if body.status == LeadStatus.DO_NOT_CONTACT and lead.seller:
            from bravo.services import optout

            for ch, val in (("sms", lead.seller.phone), ("email", lead.seller.email)):
                if val:
                    optout.record_opt_out(session, ch, val, source="staff")
        leads.apply_scoring(session, lead)
        audit.log_event(session, actor=deps.label(user), actor_type=ActorType.USER, event="lead.status_changed", entity_type="lead",
                        entity_id=lead.id, data={"from": old, "to": lead.status})
    if body.assigned_to is not None:
        lead.assigned_to = body.assigned_to
    return lead_out(lead, user.role)


@router.post("/leads/import-csv")
async def import_csv(file: UploadFile = File(...), user: User = Depends(deps.require("leads:write")), session: Session = Depends(get_session)):
    raw = await file.read(intake_agents.MAX_CSV_BYTES + 1)
    if len(raw) > intake_agents.MAX_CSV_BYTES:
        raise HTTPException(413, "file too large")
    res = intake_agents.import_csv(session, raw, actor=deps.label(user))
    return res.__dict__


@router.post("/leads/{lead_id}/rescore")
def rescore(lead_id: int, user: User = Depends(deps.require("leads:write")), session: Session = Depends(get_session)):
    lead = _lead_or_404(session, lead_id)
    leads.apply_scoring(session, lead)
    return lead_out(lead, user.role)


@router.post("/leads/{lead_id}/crm-sync")
def crm_sync_lead(lead_id: int, user: User = Depends(deps.require("leads:write")), session: Session = Depends(get_session)):
    return {"synced": crm_sync.sync_lead(session, _lead_or_404(session, lead_id), actor=deps.label(user))}


class ConsentIn(BaseModel):
    sms_consent: bool | None = None
    voice_consent: bool | None = None
    source: str = Field(min_length=3, max_length=255, description="Where/how the seller gave consent, e.g. 'web form submission 12345'")


@router.post("/leads/{lead_id}/consent")
def set_consent(lead_id: int, body: ConsentIn, user: User = Depends(deps.require("leads:write")), session: Session = Depends(get_session)):
    lead = _lead_or_404(session, lead_id)
    if body.sms_consent is None and body.voice_consent is None:
        raise HTTPException(422, "provide sms_consent and/or voice_consent")
    leads.record_consent(session, lead, sms=body.sms_consent, voice=body.voice_consent, source=body.source, actor=deps.label(user))
    crm_sync.sync_lead(session, lead, note=f"Consent updated (sms={lead.sms_consent}, voice={lead.voice_consent}): {body.source}", actor=deps.label(user))
    return {"sms_consent": lead.sms_consent, "voice_consent": lead.voice_consent, "source": lead.consent_source, "at": lead.consent_at}


class DestructiveIn(BaseModel):
    action: str = Field(pattern="^(archive_lead|purge_lead)$")
    reason: str = Field(min_length=3, max_length=500)


@router.post("/leads/{lead_id}/destructive-request")
def destructive(lead_id: int, body: DestructiveIn, user: User = Depends(deps.require("leads:write")), session: Session = Depends(get_session)):
    try:
        appr = actions.request_destructive(session, action=body.action, lead_id=lead_id, requested_by=deps.label(user), reason=body.reason)
    except actions.ActionError as exc:
        raise HTTPException(400, str(exc)) from exc
    return approval_out(appr)


# ---- intake -----------------------------------------------------------------------------------

@router.post("/intake/web-form")
def web_form(payload: dict, request: Request, session: Session = Depends(get_session), _=Depends(deps.public_rate_limit)):
    """Public endpoint for your website form. Authenticated with a shared key, not a user token."""
    key = request.headers.get("x-form-key", "")
    if not deps.safe_compare(key, get_settings().webform_key):
        raise HTTPException(401, "invalid form key")
    if payload.get("website") or payload.get("_gotcha"):  # honeypot fields
        return {"ok": True}
    payload = {str(k)[:60]: (str(v)[:2000] if v is not None else v) for k, v in list(payload.items())[:40]}
    res = intake_agents.import_web_form(session, payload)
    return {"ok": res.lead is not None, "created": res.created}


@router.post("/intake/email-poll")
def email_poll(user: User = Depends(deps.require("agents:run", expensive=True)), session: Session = Depends(get_session)):
    return _llm_guard(lambda: intake_agents.LeadIntakeAgent().poll_gmail(session).__dict__)


@router.post("/intake/crm-import")
def crm_import(user: User = Depends(deps.require("agents:run")), session: Session = Depends(get_session)):
    return intake_agents.import_from_crm(session).__dict__


# ---- conversations ----------------------------------------------------------------------------

@router.get("/leads/{lead_id}/messages")
def lead_messages(lead_id: int, user: User = Depends(deps.require("conversations:read")), session: Session = Depends(get_session)):
    _lead_or_404(session, lead_id)
    rows = session.scalars(select(Message).join(Conversation).where(Conversation.lead_id == lead_id, Message.buyer_id.is_(None)).order_by(Message.id)).all()
    return [message_out(m) for m in rows]


class DraftIn(BaseModel):
    channel: str = Field(pattern="^(sms|email)$")
    body: str = Field(min_length=1, max_length=1600)
    subject: str | None = Field(default=None, max_length=200)


@router.post("/leads/{lead_id}/messages/draft")
def draft(lead_id: int, body: DraftIn, user: User = Depends(deps.require("conversations:write")), session: Session = Depends(get_session)):
    lead = _lead_or_404(session, lead_id)
    try:
        msg = outbound.draft_message(session, lead, body.channel, body.body, subject=body.subject, created_by=deps.label(user), actor_type=ActorType.USER)
    except outbound.ComplianceError as exc:
        raise HTTPException(422, {"violations": exc.violations}) from exc
    return message_out(msg)


@router.post("/leads/{lead_id}/agent/outreach")
def agent_outreach(lead_id: int, user: User = Depends(deps.require("agents:run", expensive=True)), session: Session = Depends(get_session)):
    _lead_or_404(session, lead_id)
    run, data = _llm_guard(lambda: SellerConversationAgent().start_outreach(session, lead_id, triggered_by=deps.label(user)))
    return {"draft_message_id": data.get("draft_message_id"), "note": run.final_text, "stopped_early": run.stopped_early}


@router.post("/leads/{lead_id}/agent/reply")
def agent_reply(lead_id: int, user: User = Depends(deps.require("agents:run", expensive=True)), session: Session = Depends(get_session)):
    _lead_or_404(session, lead_id)
    run, data = _llm_guard(lambda: SellerConversationAgent().handle_reply(session, lead_id, triggered_by=deps.label(user)))
    return {"draft_message_id": data.get("draft_message_id"), "note": run.final_text, "stopped_early": run.stopped_early}


@router.post("/messages/dispatch-due")
def dispatch_due(user: User = Depends(deps.require("agents:run")), session: Session = Depends(get_session)):
    return {"messages": outbound.dispatch_due(session), "calls": voice.dispatch_calls(session)}


# ---- voice ------------------------------------------------------------------------------------

class CallIn(BaseModel):
    goal: str = Field(min_length=3, max_length=300)


@router.post("/leads/{lead_id}/calls/request")
def request_call(lead_id: int, body: CallIn, user: User = Depends(deps.require("conversations:write")), session: Session = Depends(get_session)):
    lead = _lead_or_404(session, lead_id)
    try:
        call = voice.request_call(session, lead, goal=body.goal, requested_by=deps.label(user))
    except voice.VoiceError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"call_id": call.id, "approval_id": call.approval_id, "status": call.status, "opening": call.opening}


@router.get("/leads/{lead_id}/calls")
def list_calls(lead_id: int, user: User = Depends(deps.require("conversations:read")), session: Session = Depends(get_session)):
    import json

    _lead_or_404(session, lead_id)
    rows = session.scalars(select(VoiceCall).where(VoiceCall.lead_id == lead_id).order_by(VoiceCall.id.desc())).all()
    return [{"id": c.id, "status": c.status, "goal": c.goal, "turns": c.turns, "duration_seconds": c.duration_seconds, "error": c.error,
             "scheduled_for": c.scheduled_for, "transcript": json.loads(c.transcript) if c.transcript else []} for c in rows]


# ---- approvals --------------------------------------------------------------------------------

@router.get("/approvals")
def list_approvals(status: str | None = "pending", kind: str | None = None, limit: int = Query(100, le=500),
                   user: User = Depends(deps.require("approvals:read")), session: Session = Depends(get_session)):
    stmt = select(Approval).order_by(Approval.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(Approval.status == status)
    if kind:
        stmt = stmt.where(Approval.kind == kind)
    return [approval_out(a) for a in session.scalars(stmt)]


class DecisionIn(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=1000)


@router.post("/approvals/{approval_id}/decision")
def decide(approval_id: int, body: DecisionIn, user: User = Depends(deps.current_user), session: Session = Depends(get_session)):
    try:
        appr = approvals.decide(session, approval_id, approve=body.approve, user_label=deps.label(user), user_role=user.role, note=body.note)
    except approvals.PermissionDenied as exc:
        with session_scope() as s2:  # the request transaction rolls back; keep the denial on the record
            audit.log_event(s2, actor=deps.label(user), actor_type=ActorType.USER, event="approval.denied", entity_type="approval",
                            entity_id=approval_id, data={"reason": str(exc)})
        raise HTTPException(403, str(exc)) from exc
    except approvals.ApprovalError as exc:
        raise HTTPException(409, str(exc)) from exc
    return approval_out(appr)


# ---- deals / offers / contracts / payments ----------------------------------------------------

class DealIn(BaseModel):
    address: str
    arv: Decimal
    repair_estimate: Decimal
    closing_costs: Decimal
    assignment_fee: Decimal
    discount_factor: Decimal | None = None
    arv_source: str = "user-provided estimate"
    buyer_requirements: dict | None = None
    lead_id: int | None = None


@router.post("/deals/analyze")
def analyze(body: DealIn, user: User = Depends(deps.require("deals:analyze")), session: Session = Depends(get_session)):
    if body.lead_id is not None:
        _lead_or_404(session, body.lead_id)
    try:
        result = deal_math.compute_mao(
            address=body.address, arv=body.arv, repair_estimate=body.repair_estimate, closing_costs=body.closing_costs,
            assignment_fee=body.assignment_fee, arv_source=body.arv_source[:120], buyer_requirements=body.buyer_requirements,
            discount_factor=body.discount_factor if body.discount_factor is not None else get_settings().default_discount_factor)
    except deal_math.DealInputError as exc:
        raise HTTPException(422, str(exc)) from exc
    row = save_analysis(session, result, lead_id=body.lead_id, created_by=deps.label(user))
    return {"analysis_id": row.id, "result": row.results, "summary": render_summary(row)}


class DealTextIn(BaseModel):
    text: str = Field(min_length=10, max_length=3000)
    lead_id: int | None = None


@router.post("/deals/analyze-text")
def analyze_text(body: DealTextIn, user: User = Depends(deps.require("deals:analyze", expensive=True)), session: Session = Depends(get_session)):
    return _llm_guard(lambda: DealAnalysisAgent().analyze_text(session, body.text, body.lead_id, triggered_by=deps.label(user)))


@router.get("/deals/{analysis_id}")
def get_deal(analysis_id: int, user: User = Depends(deps.require("deals:analyze")), session: Session = Depends(get_session)):
    row = session.get(DealAnalysis, analysis_id)
    if row is None:
        raise HTTPException(404, "analysis not found")
    return {"analysis_id": row.id, "lead_id": row.lead_id, "result": row.results, "summary": render_summary(row)}


class OfferIn(BaseModel):
    lead_id: int
    amount: Decimal


@router.post("/deals/{analysis_id}/offer")
def request_offer(analysis_id: int, body: OfferIn, user: User = Depends(deps.require("offers:write")), session: Session = Depends(get_session)):
    try:
        offer = actions.request_offer(session, lead_id=body.lead_id, analysis_id=analysis_id, amount=body.amount, requested_by=deps.label(user),
                                      actor_type=ActorType.USER)
    except actions.ActionError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"offer_id": offer.id, "approval_id": offer.approval_id, "status": offer.status}


class OfferMsgIn(BaseModel):
    channel: str = Field(pattern="^(sms|email)$")


@router.post("/offers/{offer_id}/draft-message")
def offer_message(offer_id: int, body: OfferMsgIn, user: User = Depends(deps.require("offers:write")), session: Session = Depends(get_session)):
    try:
        msg = actions.draft_offer_message(session, offer_id, body.channel, deps.label(user))
    except (actions.ActionError, outbound.ComplianceError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return message_out(msg)


class ContractIn(BaseModel):
    lead_id: int
    offer_id: int | None = None
    kind: str = Field(pattern="^(purchase|assignment)$")
    doc_ref: str = Field(min_length=1, max_length=255)


@router.post("/contracts")
def request_contract(body: ContractIn, user: User = Depends(deps.require("contracts:write")), session: Session = Depends(get_session)):
    _lead_or_404(session, body.lead_id)
    try:
        c = actions.request_contract(session, lead_id=body.lead_id, offer_id=body.offer_id, kind=body.kind, doc_ref=body.doc_ref,
                                     requested_by=deps.label(user))
    except actions.ActionError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"contract_id": c.id, "approval_id": c.approval_id}


class PaymentIn(BaseModel):
    lead_id: int | None = None
    amount: Decimal = Field(gt=0)
    payee: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=300)


@router.post("/payments/request")
def request_payment(body: PaymentIn, user: User = Depends(deps.require("contracts:write")), session: Session = Depends(get_session)):
    appr = actions.request_payment(session, lead_id=body.lead_id, amount=body.amount, purpose=body.purpose, payee=body.payee, requested_by=deps.label(user))
    return approval_out(appr)


# ---- buyers / disposition ---------------------------------------------------------------------

class BuyerIn(BaseModel):
    name: str
    email: str | None = None
    phone: str | None = None
    company: str | None = None
    criteria: dict = {}
    approved: bool = False
    proof_of_funds_verified: bool = False
    sms_consent: bool = False
    notes: str | None = None


@router.get("/buyers")
def list_buyers(user: User = Depends(deps.require("buyers:read")), session: Session = Depends(get_session)):
    return [buyer_out(b, user.role) for b in session.scalars(select(Buyer).order_by(Buyer.id))]


@router.post("/buyers")
def create_buyer(body: BuyerIn, user: User = Depends(deps.require("buyers:write")), session: Session = Depends(get_session)):
    try:
        b = buyers.upsert_buyer(session, actor=deps.label(user), **body.model_dump())
    except buyers.BuyerError as exc:
        raise HTTPException(422, str(exc)) from exc
    return buyer_out(b)


@router.put("/buyers/{buyer_id}")
def update_buyer(buyer_id: int, body: BuyerIn, user: User = Depends(deps.require("buyers:write")), session: Session = Depends(get_session)):
    if session.get(Buyer, buyer_id) is None:
        raise HTTPException(404, "buyer not found")
    try:
        b = buyers.upsert_buyer(session, buyer_id=buyer_id, actor=deps.label(user), **body.model_dump())
    except buyers.BuyerError as exc:
        raise HTTPException(422, str(exc)) from exc
    return buyer_out(b)


class BuyerStatusIn(BaseModel):
    approved: bool | None = None
    proof_of_funds_verified: bool | None = None
    sms_consent: bool | None = None
    active: bool | None = None


@router.patch("/buyers/{buyer_id}/status")
def buyer_status(buyer_id: int, body: BuyerStatusIn, user: User = Depends(deps.require("buyers:write")), session: Session = Depends(get_session)):
    b = session.get(Buyer, buyer_id)
    if b is None:
        raise HTTPException(404, "buyer not found")
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(b, k, v)
    audit.log_event(session, actor=deps.label(user), actor_type=ActorType.USER, event="buyer.status_changed", entity_type="buyer",
                    entity_id=b.id, data=changes)
    return buyer_out(b, user.role)


@router.get("/leads/{lead_id}/buyer-matches")
def buyer_matches(lead_id: int, analysis_id: int, include_unknown: bool = False, user: User = Depends(deps.require("buyers:read")),
                  session: Session = Depends(get_session)):
    lead, analysis = _lead_or_404(session, lead_id), session.get(DealAnalysis, analysis_id)
    if analysis is None:
        raise HTTPException(404, "analysis not found")
    try:
        ms = buyers.match_buyers(session, lead, buyers.asking_price_of(analysis), include_unknown)
    except buyers.BuyerError as exc:
        raise HTTPException(422, str(exc)) from exc
    return [{"buyer": buyer_out(m.buyer, user.role), "matched_on": m.reasons, "unverified": m.unknowns, "already_contacted": m.already_contacted} for m in ms]


class BlastIn(BaseModel):
    analysis_id: int
    buyer_ids: list[int] = Field(min_length=1, max_length=100)
    channel: str = Field(pattern="^(sms|email)$")
    reveal_street: bool = False


@router.post("/leads/{lead_id}/buyer-blast")
def buyer_blast(lead_id: int, body: BlastIn, user: User = Depends(deps.require("buyers:write")), session: Session = Depends(get_session)):
    _lead_or_404(session, lead_id)
    try:
        appr = buyers.propose_blast(session, lead_id=lead_id, analysis_id=body.analysis_id, buyer_ids=body.buyer_ids, channel=body.channel,
                                    reveal_street=body.reveal_street, requested_by=deps.label(user), actor_type=ActorType.USER)
    except buyers.BuyerError as exc:
        raise HTTPException(422, str(exc)) from exc
    return approval_out(appr)


class AgentDispoIn(BaseModel):
    analysis_id: int


@router.post("/leads/{lead_id}/agent/disposition")
def agent_disposition(lead_id: int, body: AgentDispoIn, user: User = Depends(deps.require("agents:run", expensive=True)),
                      session: Session = Depends(get_session)):
    _lead_or_404(session, lead_id)
    run = _llm_guard(lambda: BuyerDispositionAgent().disposition(session, lead_id, body.analysis_id, triggered_by=deps.label(user)))
    return {"note": run.final_text, "tool_calls": [{"tool": c["tool"], "ok": c.get("ok"), "error": c.get("error")} for c in run.tool_calls]}


class BuyerResponseIn(BaseModel):
    buyer_id: int
    status: str
    note: str | None = None


@router.post("/leads/{lead_id}/buyer-response")
def buyer_response(lead_id: int, body: BuyerResponseIn, user: User = Depends(deps.require("buyers:write")), session: Session = Depends(get_session)):
    try:
        inter = buyers.record_response(session, lead_id=lead_id, buyer_id=body.buyer_id, status=body.status, note=body.note, actor=deps.label(user))
    except buyers.BuyerError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"status": inter.status}


# ---- tasks / follow-up / campaigns / activity -------------------------------------------------

@router.get("/tasks")
def list_tasks(status: str = "open", user: User = Depends(deps.require("tasks:read")), session: Session = Depends(get_session)):
    return [task_out(t) for t in session.scalars(select(Task).where(Task.status == status).order_by(Task.priority, Task.due_at).limit(500))]


@router.post("/tasks/{task_id}/complete")
def complete_task(task_id: int, user: User = Depends(deps.require("tasks:write")), session: Session = Depends(get_session)):
    from bravo.db import utcnow

    t = session.get(Task, task_id)
    if t is None:
        raise HTTPException(404, "task not found")
    t.status, t.completed_at = "done", utcnow()
    audit.log_event(session, actor=deps.label(user), actor_type=ActorType.USER, event="task.completed", entity_type="task", entity_id=t.id)
    return task_out(t)


@router.post("/followup/run")
def run_followup(user: User = Depends(deps.require("agents:run")), session: Session = Depends(get_session)):
    return FollowUpAgent().run_cycle(session)


class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    lead_ids: list[int] = Field(min_length=1, max_length=followup.MAX_CAMPAIGN_SIZE)
    template: str = Field(min_length=5, max_length=600)
    channel: str = Field(pattern="^(sms|email)$")
    subject: str | None = Field(default=None, max_length=200)


@router.post("/campaigns")
def campaign(body: CampaignIn, user: User = Depends(deps.require("campaigns:write")), session: Session = Depends(get_session)):
    try:
        appr = followup.propose_campaign(session, name=body.name, lead_ids=body.lead_ids, template=body.template, channel=body.channel,
                                         subject=body.subject, requested_by=deps.label(user), actor_type=ActorType.USER)
    except followup.CampaignError as exc:
        raise HTTPException(422, str(exc)) from exc
    return approval_out(appr)


@router.get("/appointments")
def list_appointments(user: User = Depends(deps.require("leads:read")), session: Session = Depends(get_session)):
    return [{"id": a.id, "lead_id": a.lead_id, "title": a.title, "starts_at": a.starts_at, "ends_at": a.ends_at, "status": a.status}
            for a in session.scalars(select(Appointment).order_by(Appointment.starts_at).limit(200))]


@router.get("/activity")
def activity(entity_type: str | None = None, entity_id: int | None = None, event: str | None = None, limit: int = Query(100, le=500),
             user: User = Depends(deps.require("activity:read")), session: Session = Depends(get_session)):
    stmt = select(ActivityLog).order_by(ActivityLog.id.desc()).limit(limit)
    for col, val in ((ActivityLog.entity_type, entity_type), (ActivityLog.entity_id, entity_id), (ActivityLog.event, event)):
        if val is not None:
            stmt = stmt.where(col == val)
    return [{"id": a.id, "at": a.created_at, "actor_type": a.actor_type, "actor": a.actor, "event": a.event,
             "entity_type": a.entity_type, "entity_id": a.entity_id, "data": a.data} for a in session.scalars(stmt)]


# ---- Bravo Vision sync ------------------------------------------------------------------------

def _sync_key(request: Request, _=Depends(deps.public_rate_limit)) -> None:
    if not deps.safe_compare(request.headers.get("x-sync-key", ""), get_settings().bravo_sync_key):
        raise HTTPException(401, "invalid sync key")


class BravoLeadsIn(BaseModel):
    leads: list[dict] = Field(min_length=1, max_length=bravo_sync.MAX_BATCH)


@router.post("/sync/bravo/leads")
def sync_in(body: BravoLeadsIn, _=Depends(_sync_key), session: Session = Depends(get_session)):
    """Bravo Vision pushes leads here. Idempotent on external_id; consent fields are honored only when explicitly present."""
    results = []
    for item in body.leads:
        with session.begin_nested():
            results.append(bravo_sync.upsert_lead(session, {str(k)[:60]: (v if not isinstance(v, str) else v[:5000]) for k, v in item.items()}))
    return {"results": results}


@router.get("/sync/bravo/leads")
def sync_out(since: datetime | None = None, limit: int = Query(500, le=1000), _=Depends(_sync_key), session: Session = Depends(get_session)):
    """Status feed for Bravo Vision to pull. Contains no seller contact details."""
    return {"leads": bravo_sync.export_since(session, since, limit)}


# ---- Twilio voice webhooks ---------------------------------------------------------------------

def _signed_url(request: Request) -> str:
    base = get_settings().public_base_url.rstrip("/")
    q = f"?{request.url.query}" if request.url.query else ""
    return f"{base}{request.url.path}{q}" if base else str(request.url)


def _voice_twiml(say: str, hangup: bool, turn_url: str) -> Response:
    from xml.sax.saxutils import escape, quoteattr

    if hangup or not say:
        body = (f'<Say>{escape(say)}</Say>' if say else "") + "<Hangup/>"
    else:
        body = (f'<Gather input="speech" action={quoteattr(turn_url)} method="POST" speechTimeout="auto" timeout="6" language="en-US">'
                f"<Say>{escape(say)}</Say></Gather><Say>Sorry, I didn't catch that. We'll try again another time. Goodbye.</Say><Hangup/>")
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>', media_type="application/xml")


@router.post("/webhooks/twilio/voice-turn")
async def twilio_voice_turn(request: Request, call_id: int, session: Session = Depends(get_session), _=Depends(deps.public_rate_limit)):
    form = {k: str(v) for k, v in (await request.form()).items()}
    if not valid_twilio_signature(_signed_url(request), form, request.headers.get("x-twilio-signature", "")):
        raise HTTPException(403, "invalid Twilio signature")
    out = voice.handle_turn(session, call_id, form.get("SpeechResult"))
    return _voice_twiml(out["say"], out["hangup"], _signed_url(request).split("?")[0] + f"?call_id={call_id}")


@router.post("/webhooks/twilio/voice-status")
async def twilio_voice_status(request: Request, call_id: int, session: Session = Depends(get_session), _=Depends(deps.public_rate_limit)):
    form = {k: str(v) for k, v in (await request.form()).items()}
    if not valid_twilio_signature(_signed_url(request), form, request.headers.get("x-twilio-signature", "")):
        raise HTTPException(403, "invalid Twilio signature")
    dur = form.get("CallDuration")
    voice.handle_status(session, call_id, form.get("CallStatus", ""), int(dur) if dur and dur.isdigit() else None)
    return Response(status_code=204)


# ---- Twilio webhook ---------------------------------------------------------------------------

def _twiml(message: str | None = None) -> Response:
    from xml.sax.saxutils import escape

    body = f"<Message>{escape(message)}</Message>" if message else ""
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>', media_type="application/xml")


@router.post("/webhooks/twilio/sms")
async def twilio_sms(request: Request, session: Session = Depends(get_session), _=Depends(deps.public_rate_limit)):
    form = {k: str(v) for k, v in (await request.form()).items()}
    base = get_settings().public_base_url.rstrip("/")
    url = f"{base}{request.url.path}" if base else str(request.url)
    if not valid_twilio_signature(url, form, request.headers.get("x-twilio-signature", "")):
        raise HTTPException(403, "invalid Twilio signature")
    res = inbound.handle_inbound_sms(session, form.get("From", ""), form.get("Body", "")[:1600], form.get("MessageSid"))
    return _twiml(res.reply)
