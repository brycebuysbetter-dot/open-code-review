"""Cash-buyer matching and privacy-safe deal summaries. Matching uses only facts we actually hold."""
import re
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from bravo import approvals, audit
from bravo.config import get_settings
from bravo.crypto import get_crypto, mask_email, mask_phone
from bravo.enums import ActorType, ApprovalKind, MessageStatus
from bravo.models import Buyer, BuyerInteraction, DealAnalysis, Lead, Message
from bravo.services import compliance, outbound
from bravo.services.normalize import normalize_email, normalize_phone
from bravo.services.optout import is_opted_out

CRITERIA_KEYS = {"states", "cities", "zips", "property_types", "min_beds", "min_baths", "min_sqft", "max_price"}
RESPONSE_STATUSES = {"interested", "passed", "question", "no_response"}


class BuyerError(Exception):
    pass


def clean_criteria(criteria: dict | None) -> dict:
    criteria = criteria or {}
    bad = set(criteria) - CRITERIA_KEYS
    if bad:
        raise BuyerError(f"unknown criteria: {sorted(bad)}")
    out = {}
    for k, v in criteria.items():
        if k in ("states", "cities", "zips", "property_types"):
            out[k] = [str(x).strip().upper() for x in (v if isinstance(v, list) else [v]) if str(x).strip()]
        elif v not in (None, ""):
            out[k] = float(v)
    return out


def upsert_buyer(session: Session, *, name: str, email: str | None = None, phone: str | None = None, company: str | None = None,
                 criteria: dict | None = None, approved: bool = False, proof_of_funds_verified: bool = False, sms_consent: bool = False,
                 notes: str | None = None, buyer_id: int | None = None, actor: str = "system") -> Buyer:
    if not name.strip():
        raise BuyerError("name is required")
    e, p = normalize_email(email), normalize_phone(phone)
    if not (e or p):
        raise BuyerError("a valid email or phone is required")
    c = get_crypto()
    b = session.get(Buyer, buyer_id) if buyer_id else None
    if b is None and e:
        b = session.scalar(select(Buyer).where(Buyer.email_hash == c.blind_index(e)))
    if b is None:
        b = Buyer(name=name.strip())
        session.add(b)
    b.name, b.company, b.notes = name.strip(), company, notes
    b.email, b.email_hash = e, c.blind_index(e) if e else None
    b.phone, b.phone_hash = p, c.blind_index(p) if p else None
    b.criteria = clean_criteria(criteria)
    b.approved, b.proof_of_funds_verified, b.sms_consent = approved, proof_of_funds_verified, sms_consent
    session.flush()
    audit.log_event(session, actor=actor, event="buyer.upserted", entity_type="buyer", entity_id=b.id,
                    data={"approved": approved, "pof": proof_of_funds_verified})
    return b


@dataclass
class Match:
    buyer: Buyer
    reasons: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    already_contacted: bool = False


def match_buyers(session: Session, lead: Lead, asking_price: Decimal | None = None, include_unknown: bool = False) -> list[Match]:
    prop = lead.property
    if prop is None:
        raise BuyerError("lead has no property on file")
    contacted = set(session.scalars(select(BuyerInteraction.buyer_id).where(BuyerInteraction.lead_id == lead.id)))
    results = []
    for b in session.scalars(select(Buyer).where(Buyer.active.is_(True), Buyer.approved.is_(True), Buyer.proof_of_funds_verified.is_(True))):
        crit, reasons, unknowns, failed = b.criteria or {}, [], [], False

        def check(label, known, ok_fn, ok_text):
            nonlocal failed
            if not known:
                unknowns.append(f"{label} unknown for this property")
            elif ok_fn():
                reasons.append(ok_text)
            else:
                failed = True

        if crit.get("states"):
            check("state", prop.state, lambda: prop.state.upper() in crit["states"], f"state {prop.state}")
        if crit.get("cities"):
            check("city", prop.city, lambda: prop.city.upper() in crit["cities"], f"city {prop.city}")
        if crit.get("zips"):
            check("zip", prop.zip, lambda: prop.zip in crit["zips"], f"zip {prop.zip}")
        if crit.get("property_types"):
            check("property type", prop.property_type, lambda: prop.property_type.upper() in crit["property_types"], f"type {prop.property_type}")
        if "min_beds" in crit:
            check("beds", prop.beds, lambda: prop.beds >= crit["min_beds"], f"{prop.beds}+ beds")
        if "min_baths" in crit:
            check("baths", prop.baths, lambda: float(prop.baths) >= crit["min_baths"], f"{prop.baths}+ baths")
        if "min_sqft" in crit:
            check("sqft", prop.sqft, lambda: prop.sqft >= crit["min_sqft"], f"{prop.sqft}+ sqft")
        if "max_price" in crit:
            check("asking price", asking_price, lambda: float(asking_price) <= crit["max_price"], "asking price within buyer budget")
        if failed or not reasons or (unknowns and not include_unknown):
            continue
        results.append(Match(b, reasons, unknowns, b.id in contacted))
    return sorted(results, key=lambda m: (m.already_contacted, -len(m.reasons), m.buyer.id))


def asking_price_of(analysis: DealAnalysis) -> Decimal:
    return Decimal(analysis.results["mao"]) + Decimal(analysis.inputs["assignment_fee"])


def leaks_seller_info(lead: Lead, body: str, allow_street: bool = False) -> list[str]:
    """Defense in depth: nothing about the seller may appear in a buyer-facing message."""
    low, digits, found = body.lower(), re.sub(r"\D", "", body), []
    s = lead.seller
    if s:
        for tok in (s.name or "").lower().split():
            if len(tok) >= 3 and re.search(rf"\b{re.escape(tok)}\b", low):
                found.append("seller name")
                break
        if s.phone and re.sub(r"\D", "", s.phone)[-10:] in digits:
            found.append("seller phone")
        if s.email and s.email.lower() in low:
            found.append("seller email")
        if s.mailing_address and s.mailing_address.lower() in low:
            found.append("seller mailing address")
    for k, v in (lead.discovery or {}).items():
        if isinstance(v, str) and len(v.strip()) >= 6 and v.strip().lower() in low:
            found.append(f"seller {k}")
    if not allow_street and lead.property:
        parts = lead.property.street.lower().split()
        if parts and (lead.property.street.lower() in low or (
                len(parts) > 1 and re.search(rf"\b{re.escape(parts[0])}\s+{re.escape(parts[1].rstrip('.'))}", low))):
            found.append("street address")
    return found


def build_deal_summary(lead: Lead, analysis: DealAnalysis, buyer_name: str, reveal_street: bool = False) -> tuple[str, str]:
    p = lead.property
    if p is None:
        raise BuyerError("lead has no property")
    price = asking_price_of(analysis)
    r = analysis.inputs
    where = ", ".join(x for x in (p.street if reveal_street else None, p.city, p.state, p.zip) if x) or "location available on request"
    facts = [f"Location: {where}"]
    if p.property_type:
        facts.append(f"Type: {p.property_type}")
    if p.beds is not None or p.baths is not None:
        facts.append(f"Beds/Baths: {p.beds if p.beds is not None else '?'}/{p.baths if p.baths is not None else '?'}")
    if p.sqft:
        facts.append(f"Sqft: {p.sqft:,} (per our records; verify)")
    if p.year_built:
        facts.append(f"Year built: {p.year_built} (per our records; verify)")
    first = buyer_name.split()[0]
    closing = ("Reply if you'd like more details or a showing; we'll share the full address and next steps."
               if not reveal_street else "Reply with any questions or to schedule a showing.")
    body = (
        f"Hi {first}, we have an off-market opportunity that may fit your criteria.\n\n"
        + "\n".join(f"- {f}" for f in facts)
        + f"\n- Asking price: ${price:,.0f}\n"
        f"- ARV (our estimate, unverified): ${Decimal(r['arv']):,.0f}\n"
        f"- Repair estimate (our estimate, unverified): ${Decimal(r['repair_estimate']):,.0f}\n\n"
        "All figures are estimates only, not an appraisal or guarantee of value, condition, or profit. "
        f"Please do your own due diligence. {closing}"
    )
    return "Off-market deal that may fit your criteria", body


def propose_blast(session: Session, *, lead_id: int, analysis_id: int, buyer_ids: list[int], channel: str, requested_by: str,
                  reveal_street: bool = False, actor_type: ActorType = ActorType.AGENT):
    lead, analysis = session.get(Lead, lead_id), session.get(DealAnalysis, analysis_id)
    if lead is None or analysis is None or analysis.lead_id not in (None, lead_id):
        raise BuyerError("lead/analysis mismatch")
    if not analysis.results.get("viable"):
        raise BuyerError("analysis is not viable (MAO <= 0); do not market this deal")
    approval = approvals.request_approval(
        session, kind=ApprovalKind.BUYER_BLAST, requested_by=requested_by, actor_type=actor_type, entity_type="lead",
        entity_id=lead_id, summary="buyer blast", payload={"items": []})
    items, skipped, subject = [], {}, None
    for bid in dict.fromkeys(buyer_ids):
        b = session.get(Buyer, bid)
        why = None
        to = (b.phone if channel == "sms" else b.email) if b else None
        if b is None or not (b.active and b.approved and b.proof_of_funds_verified):
            why = "not_approved_cash_buyer"
        elif not to:
            why = "no_contact"
        elif is_opted_out(session, channel, to):
            why = "opted_out"
        elif channel == "sms" and get_settings().consent_required and not b.sms_consent:
            why = "no_consent"
        elif session.scalar(select(BuyerInteraction.id).where(BuyerInteraction.lead_id == lead_id, BuyerInteraction.buyer_id == bid,
                                                              BuyerInteraction.status != "cancelled")):
            why = "already_contacted"
        if why:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        subject, body = build_deal_summary(lead, analysis, b.name, reveal_street)
        leaks = leaks_seller_info(lead, body, allow_street=reveal_street)
        if leaks:
            raise BuyerError("summary would expose seller information: " + ", ".join(leaks))
        problems = compliance.check_message(body, channel, allow_amounts=True)
        if problems:
            raise BuyerError("summary fails compliance: " + "; ".join(problems))
        conv = outbound.get_conversation(session, lead_id, f"buyer_{channel}")
        msg = Message(conversation_id=conv.id, direction="outbound", channel=channel, subject=subject,
                      body=compliance.with_footer(body, channel), status=MessageStatus.PENDING_APPROVAL, created_by=requested_by,
                      buyer_id=b.id, approval_id=approval.id)
        session.add(msg)
        session.flush()
        session.add(BuyerInteraction(lead_id=lead_id, buyer_id=b.id, status="drafted", message_id=msg.id,
                                     match_reasons=[]))
        items.append({"message_id": msg.id, "lead_id": lead_id, "buyer_id": b.id,
                      "to": mask_phone(to) if channel == "sms" else mask_email(to), "body": msg.body})
    if not items:
        raise BuyerError(f"no eligible buyers (skipped: {skipped})")
    approval.payload = {"lead_id": lead_id, "analysis_id": analysis_id, "channel": channel, "reveal_street": reveal_street,
                        "subject": subject, "items": items, "skipped": skipped}
    approval.payload_hash = approvals.payload_digest(approval.payload)
    approval.summary = f"Send deal summary to {len(items)} approved cash buyers ({channel}) for lead #{lead_id}"
    return approval


def record_response(session: Session, *, lead_id: int, buyer_id: int, status: str, note: str | None, actor: str,
                    actor_type: ActorType = ActorType.USER) -> BuyerInteraction:
    if status not in RESPONSE_STATUSES:
        raise BuyerError(f"status must be one of {sorted(RESPONSE_STATUSES)}")
    inter = session.scalar(select(BuyerInteraction).where(BuyerInteraction.lead_id == lead_id, BuyerInteraction.buyer_id == buyer_id))
    if inter is None or inter.status in ("drafted", "cancelled", "matched"):
        raise BuyerError("no outreach has been sent to this buyer for this deal; cannot record a response")
    inter.status, inter.note = status, (note or "")[:1000]
    audit.log_event(session, actor=actor, actor_type=actor_type, event="buyer.response_recorded", entity_type="lead", entity_id=lead_id,
                    data={"buyer_id": buyer_id, "status": status})
    if status in ("interested", "question"):
        from bravo.models import Task

        session.add(Task(lead_id=lead_id, kind="buyer_response", title=f"Buyer #{buyer_id} {status}: follow up", priority=1, created_by=actor))
    return inter
