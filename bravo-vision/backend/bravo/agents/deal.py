"""Deal Analysis Agent. Arithmetic is code; the LLM only parses a request and every number it passes must be user-supplied."""
import re
from decimal import Decimal

from sqlalchemy.orm import Session

from bravo import audit
from bravo.agents.base import Agent, AgentContext, Tool, ToolError
from bravo.config import get_settings
from bravo.models import DealAnalysis, Lead
from bravo.services import actions, deal_math

_NUM_RE = re.compile(r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([km]|thousand|million)?\b", re.I)
_MULT = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000}


def numbers_in(text: str) -> set[Decimal]:
    out = set()
    for m in _NUM_RE.finditer(text):
        base = Decimal(m.group(1).replace(",", ""))
        out.add(base * _MULT.get((m.group(2) or "").lower(), 1))
        out.add(base)
    return out


def save_analysis(session: Session, result: deal_math.DealResult, *, lead_id: int | None, created_by: str) -> DealAnalysis:
    d = result.to_dict()
    row = DealAnalysis(lead_id=lead_id, address=result.inputs["address"], inputs=d["inputs"], results=d, assumptions=d["assumptions"],
                       created_by=created_by)
    session.add(row)
    session.flush()
    audit.log_event(session, actor=created_by, event="deal.analyzed", entity_type="deal_analysis", entity_id=row.id,
                    data={"mao": d["mao"], "viable": d["viable"], "lead_id": lead_id})
    return row


def render_summary(row: DealAnalysis) -> str:
    r = row.results
    lines = [f"Deal analysis for {row.address} (analysis #{row.id})", ""]
    lines += [f"  {b['label']}: {b['amount']}" for b in r["breakdown"]]
    if r.get("buyer_check"):
        bc = r["buyer_check"]
        lines += ["", f"Buyer-fit check (buyer price {bc['buyer_price']}): " + ("all requirements met" if bc["all_met"] else "NOT all requirements met")]
        lines += [f"  - {c['requirement']}: {'met' if c['met'] else 'not met'}" for c in bc["checks"]]
    lines += ["", "Assumptions:"] + [f"  - {a}" for a in r["assumptions"]]
    if r["warnings"]:
        lines += ["", "Warnings:"] + [f"  - {w}" for w in r["warnings"]]
    lines += ["", r["disclaimer"]]
    return "\n".join(lines)


class DealAnalysisAgent(Agent):
    name = "deal_analysis"
    role_prompt = """\
Role: Deal Analysis. The user describes a property deal in plain language. Call calculate_mao with the address and the
numbers the user gave: ARV, repair estimate, closing costs, desired assignment fee, and optional buyer requirements
(max_price, max_pct_of_arv). Pass numbers exactly as the user stated them; if any required number is missing, ask the
user for it instead of guessing or assuming. You may not look up or invent comps or ARV. The system computes the maximum
allowable offer and prints the breakdown and assumptions itself, so do not restate calculations; reply with at most one
sentence. Only call request_offer if the user explicitly asks to propose a specific offer on a known lead."""

    def tools(self, ctx: AgentContext) -> list[Tool]:
        def calculate(c: AgentContext, args: dict) -> dict:
            allowed = numbers_in(c.data["user_text"])
            for key in ("arv", "repair_estimate", "closing_costs", "assignment_fee"):
                try:
                    val = Decimal(str(args.get(key, "")).replace(",", "").replace("$", ""))
                except Exception as exc:
                    raise ToolError(f"{key} is required and must be a number; ask the user") from exc
                if val not in allowed:
                    raise ToolError(f"{key}={val} does not appear in the user's message; ask the user for it. Never assume figures.")
            factor = args.get("discount_factor")
            if factor is not None and Decimal(str(factor)) not in allowed and Decimal(str(factor)) != Decimal(str(get_settings().default_discount_factor)):
                raise ToolError("discount_factor was not given by the user; omit it to use the business default")
            try:
                result = deal_math.compute_mao(
                    address=str(args.get("address", "")), arv=args.get("arv"), repair_estimate=args.get("repair_estimate"),
                    closing_costs=args.get("closing_costs"), assignment_fee=args.get("assignment_fee"),
                    discount_factor=factor if factor is not None else get_settings().default_discount_factor,
                    arv_source=str(args.get("arv_source") or "user-provided estimate")[:120],
                    buyer_requirements=args.get("buyer_requirements"))
            except deal_math.DealInputError as exc:
                raise ToolError(str(exc)) from exc
            row = save_analysis(c.session, result, lead_id=c.data.get("lead_id"), created_by=c.actor)
            c.data["analysis"] = row
            return {"analysis_id": row.id, "mao": row.results["mao"], "viable": row.results["viable"]}

        def request_offer(c: AgentContext, args: dict) -> dict:
            row = c.data.get("analysis")
            lead_id = c.data.get("lead_id")
            if row is None or not lead_id:
                raise ToolError("an analysis and a lead are required before requesting an offer")
            if Decimal(str(args.get("amount", ""))) not in numbers_in(c.data["user_text"]):
                raise ToolError("offer amount must be stated by the user")
            try:
                offer = actions.request_offer(c.session, lead_id=lead_id, analysis_id=row.id, amount=args["amount"], requested_by=c.actor)
            except actions.ActionError as exc:
                raise ToolError(str(exc)) from exc
            return {"offer_id": offer.id, "approval_id": offer.approval_id, "note": "awaiting human approval"}

        num = {"type": ["number", "string"]}
        return [
            Tool("calculate_mao", "Compute maximum allowable offer from user-provided inputs.", {
                "type": "object", "required": ["address", "arv", "repair_estimate", "closing_costs", "assignment_fee"],
                "properties": {"address": {"type": "string"}, "arv": num, "repair_estimate": num, "closing_costs": num,
                               "assignment_fee": num, "discount_factor": num, "arv_source": {"type": "string"},
                               "buyer_requirements": {"type": "object", "properties": {"max_price": num, "max_pct_of_arv": num}}}},
                 calculate),
            Tool("request_offer", "Queue an offer (at or below MAO) for human approval.",
                 {"type": "object", "required": ["amount"], "properties": {"amount": num}}, request_offer),
        ]

    def analyze_text(self, session: Session, user_text: str, lead_id: int | None = None, triggered_by: str = "system") -> dict:
        if lead_id is not None and session.get(Lead, lead_id) is None:
            raise ValueError("lead not found")
        data = {"user_text": user_text, "lead_id": lead_id}
        run = self.run(session, f"<request>\n{user_text[:3000]}\n</request>", triggered_by=triggered_by, data=data)
        row = data.get("analysis")
        return {"analysis_id": row.id if row else None, "summary": render_summary(row) if row else None,
                "message": None if row else run.final_text, "tool_errors": [c["error"] for c in run.tool_calls if not c.get("ok")]}

