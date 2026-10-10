"""Buyer Disposition Agent: match approved cash buyers, queue deal summaries, classify replies."""
from decimal import Decimal

from sqlalchemy.orm import Session

from bravo.agents.base import Agent, AgentContext, Tool, ToolError
from bravo.enums import ActorType
from bravo.models import DealAnalysis, Lead
from bravo.services import buyers


class BuyerDispositionAgent(Agent):
    name = "buyer_disposition"
    role_prompt = """\
Role: Buyer Disposition. For a lead with a saved deal analysis: call find_matching_buyers, then propose_buyer_blast with the buyer_ids
that fit (only approved, proof-of-funds-verified buyers can be returned; never invent buyers or buyer interest). The system writes the
buyer-facing summary itself from stored facts and never includes seller information. Do not reveal the street address unless the user
explicitly asked. When given a buyer's reply, call record_buyer_response with a status and a short verbatim quote from that reply
as evidence; if the reply is ambiguous use status "question". Report briefly."""

    def tools(self, ctx: AgentContext) -> list[Tool]:
        def lead_and_analysis(c: AgentContext):
            lead = c.session.get(Lead, c.data["lead_id"])
            analysis = c.session.get(DealAnalysis, c.data["analysis_id"]) if c.data.get("analysis_id") else None
            if lead is None:
                raise ToolError("lead not found")
            return lead, analysis

        def find(c: AgentContext, a: dict) -> dict:
            lead, analysis = lead_and_analysis(c)
            price = buyers.asking_price_of(analysis) if analysis else (Decimal(str(a["asking_price"])) if a.get("asking_price") else None)
            try:
                ms = buyers.match_buyers(c.session, lead, price, include_unknown=bool(a.get("include_unknown")))
            except buyers.BuyerError as exc:
                raise ToolError(str(exc)) from exc
            return {"matches": [{"buyer_id": m.buyer.id, "name": m.buyer.name, "matched_on": m.reasons,
                                 "unverified": m.unknowns, "already_contacted": m.already_contacted} for m in ms],
                    "note": "only approved, proof-of-funds-verified buyers are considered"}

        def blast(c: AgentContext, a: dict) -> dict:
            lead, analysis = lead_and_analysis(c)
            if analysis is None:
                raise ToolError("a saved deal analysis is required")
            try:
                appr = buyers.propose_blast(c.session, lead_id=lead.id, analysis_id=analysis.id, buyer_ids=[int(x) for x in a.get("buyer_ids", [])],
                                            channel=str(a.get("channel", "email")), requested_by=c.actor,
                                            reveal_street=bool(c.data.get("allow_street") and a.get("reveal_street")))
            except buyers.BuyerError as exc:
                raise ToolError(str(exc)) from exc
            return {"approval_id": appr.id, "summary": appr.summary, "note": "awaiting human approval; nothing sent"}

        def response(c: AgentContext, a: dict) -> dict:
            reply = c.data.get("buyer_reply")
            if not reply or c.data.get("buyer_id") != a.get("buyer_id"):
                raise ToolError("no buyer reply is in context for this buyer")
            quote = str(a.get("quote", "")).strip().lower()
            if len(quote) < 3 or quote not in reply.lower():
                raise ToolError("quote must be copied verbatim from the buyer's reply")
            lead, _ = lead_and_analysis(c)
            try:
                inter = buyers.record_response(c.session, lead_id=lead.id, buyer_id=int(a["buyer_id"]), status=str(a.get("status")),
                                               note=quote, actor=c.actor, actor_type=ActorType.AGENT)
            except buyers.BuyerError as exc:
                raise ToolError(str(exc)) from exc
            return {"recorded": inter.status}

        ids = {"type": "array", "items": {"type": "integer"}}
        return [
            Tool("find_matching_buyers", "Match approved cash buyers to this deal using stored property facts.",
                 {"type": "object", "properties": {"include_unknown": {"type": "boolean"}}}, find),
            Tool("propose_buyer_blast", "Queue the deal summary to chosen buyers for human approval.", {
                "type": "object", "required": ["buyer_ids", "channel"],
                "properties": {"buyer_ids": ids, "channel": {"type": "string", "enum": ["sms", "email"]}, "reveal_street": {"type": "boolean"}}}, blast),
            Tool("record_buyer_response", "Record a buyer's reply status with a verbatim quote as evidence.", {
                "type": "object", "required": ["buyer_id", "status", "quote"],
                "properties": {"buyer_id": {"type": "integer"}, "status": {"type": "string", "enum": sorted(buyers.RESPONSE_STATUSES)},
                               "quote": {"type": "string"}}}, response),
        ]

    def disposition(self, session: Session, lead_id: int, analysis_id: int, triggered_by: str = "system", allow_street: bool = False):
        data = {"lead_id": lead_id, "analysis_id": analysis_id, "allow_street": allow_street}
        run = self.run(session, f"Find matching approved cash buyers for lead #{lead_id} and queue the deal summary for approval.",
                       triggered_by=triggered_by, data=data)
        return run

    def handle_buyer_reply(self, session: Session, lead_id: int, buyer_id: int, reply_text: str, triggered_by: str = "system"):
        data = {"lead_id": lead_id, "buyer_id": buyer_id, "buyer_reply": reply_text}
        return self.run(session, f"<buyer_reply buyer_id=\"{buyer_id}\">\n{reply_text[:3000]}\n</buyer_reply>\nClassify and record it.",
                        triggered_by=triggered_by, data=data)
