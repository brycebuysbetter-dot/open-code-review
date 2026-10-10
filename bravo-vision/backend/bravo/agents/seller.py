"""Seller Conversation Agent: discovery, drafting, summarizing, CRM updates. Never sends."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from bravo import audit
from bravo.agents.base import Agent, AgentContext, Tool, ToolError
from bravo.db import utcnow
from bravo.enums import ActorType, LeadStatus, MessageStatus
from bravo.integrations import registry
from bravo.models import Appointment, Lead, Message
from bravo.services import crm_sync, leads, optout, outbound

DISCOVERY_TOPICS = {
    "timeline": "how soon they want to sell",
    "condition": "the property's condition and any repairs needed",
    "mortgage": "whether there is a mortgage and whether payments are current",
    "occupancy": "who lives in or uses the property (owner, tenant, vacant)",
    "price_expectation": "what price they have in mind",
    "reason": "their reason for selling",
}
SETTABLE_STATUSES = {LeadStatus.ENGAGED, LeadStatus.NURTURE, LeadStatus.DEAD, LeadStatus.DO_NOT_CONTACT}


def _first_name(lead: Lead) -> str | None:
    name = lead.seller.name if lead.seller else None
    return name.split()[0] if name else None


class SellerConversationAgent(Agent):
    name = "seller_conversation"
    role_prompt = """\
Role: Seller Conversation. You help a human acquisitions team talk with property sellers by text and email.
Workflow each time: (1) call get_lead_context; (2) call record_discovery with anything the seller has actually stated;
(3) decide the next step and, unless the seller asked to stop or the conversation is finished, call draft_reply with a
short, warm, plain-language message that asks for at most two of the still-missing topics (timeline, condition, mortgage,
occupancy, price expectations, reason for selling), one conversational question at a time where possible; (4) call
save_summary with a 2-4 sentence factual summary of the whole conversation and what is still unknown.
Message rules: no dollar figures, no promises or guarantees, no pressure or urgency tactics, no legal/tax/lending advice
(for those say they should consult a qualified professional). If asked about price, say a team member will review the
details and follow up; never name a number. SMS: under 320 characters. Do not add an opt-out line (the system adds it).
If the seller asks to stop or says they are not interested, call update_lead_status (do_not_contact or dead) and do NOT draft a reply.
Your draft is only a proposal; a human must approve it before anything is sent."""

    def tools(self, ctx: AgentContext) -> list[Tool]:
        def lead_of(c: AgentContext) -> Lead:
            lead = c.session.get(Lead, c.data["lead_id"])
            if lead is None:
                raise ToolError("lead not found")
            return lead

        def get_context(c: AgentContext, args: dict) -> dict:
            lead = lead_of(c)
            msgs = c.session.scalars(
                select(Message).join(outbound.Conversation).where(outbound.Conversation.lead_id == lead.id, Message.buyer_id.is_(None))
                .order_by(Message.id.desc()).limit(20)).all()[::-1]
            known = {k: v for k, v in (lead.discovery or {}).items() if v}
            return {
                "lead_id": lead.id, "status": lead.status, "seller_first_name": _first_name(lead),
                "property": ({"street": lead.property.street, "city": lead.property.city, "state": lead.property.state}
                             if lead.property else None),
                "channels_available": [ch for ch in ("sms", "email") if outbound.recipient(lead, ch)],
                "known_discovery": known,
                "missing_topics": {k: v for k, v in DISCOVERY_TOPICS.items() if k not in known},
                "conversation": [{"direction": m.direction, "status": m.status,
                                  "text": f"<untrusted_seller_text>{m.body}</untrusted_seller_text>" if m.direction == "inbound" else m.body}
                                 for m in msgs],
            }

        def record_discovery(c: AgentContext, args: dict) -> dict:
            lead = lead_of(c)
            updates = {k: str(v).strip() for k, v in args.items() if k in DISCOVERY_TOPICS and v and str(v).strip()}
            if not updates:
                raise ToolError("provide at least one of: " + ", ".join(DISCOVERY_TOPICS))
            lead.discovery = {**(lead.discovery or {}), **updates}
            leads.apply_scoring(c.session, lead)
            audit.log_event(c.session, actor=c.actor, actor_type=ActorType.AGENT, event="lead.discovery_updated",
                            entity_type="lead", entity_id=lead.id, data={"fields": list(updates), "score": lead.motivation_score})
            return {"updated": list(updates), "motivation_score": lead.motivation_score, "priority": lead.priority}

        def draft_reply(c: AgentContext, args: dict) -> dict:
            lead = lead_of(c)
            channel = args.get("channel")
            if channel not in ("sms", "email"):
                raise ToolError("channel must be 'sms' or 'email'")
            pending = c.session.scalar(
                select(Message.id).join(outbound.Conversation).where(
                    outbound.Conversation.lead_id == lead.id, Message.buyer_id.is_(None), Message.channel == channel, Message.direction == "outbound",
                    Message.status == MessageStatus.PENDING_APPROVAL))
            if pending:
                raise ToolError("a draft for this lead and channel is already awaiting approval")
            try:
                msg = outbound.draft_message(c.session, lead, channel, str(args.get("body", "")), subject=args.get("subject"),
                                             created_by=c.actor)
            except outbound.ComplianceError as exc:
                raise ToolError("draft rejected by compliance: " + "; ".join(exc.violations) + ". Revise and try again.") from exc
            c.data["draft_message_id"] = msg.id
            return {"message_id": msg.id, "status": msg.status, "approval_id": msg.approval_id,
                    "note": "awaiting human approval; not sent"}

        def save_summary(c: AgentContext, args: dict) -> dict:
            lead = lead_of(c)
            summary = str(args.get("summary", "")).strip()
            if len(summary) < 10:
                raise ToolError("summary too short")
            channel = args.get("channel") or "sms"
            conv = outbound.get_conversation(c.session, lead.id, channel if channel in ("sms", "email") else "sms")
            conv.summary = summary[:2000]
            synced = crm_sync.sync_lead(c.session, lead, note=f"Jarvis summary: {summary[:1500]}", actor=c.actor)
            c.data["summarized"] = True
            return {"saved": True, "crm_synced": synced}

        def update_status(c: AgentContext, args: dict) -> dict:
            lead = lead_of(c)
            try:
                new = LeadStatus(args.get("status"))
            except ValueError as exc:
                raise ToolError("invalid status") from exc
            if new not in SETTABLE_STATUSES:
                raise ToolError("allowed statuses: " + ", ".join(sorted(x.value for x in SETTABLE_STATUSES)))
            old = lead.status
            lead.status = new
            if new == LeadStatus.DO_NOT_CONTACT:
                for ch in ("sms", "email"):
                    to = outbound.recipient(lead, ch)
                    if to:
                        optout.record_opt_out(c.session, ch, to, source="seller_request")
            leads.apply_scoring(c.session, lead)
            audit.log_event(c.session, actor=c.actor, actor_type=ActorType.AGENT, event="lead.status_changed",
                            entity_type="lead", entity_id=lead.id, data={"from": old, "to": str(new), "reason": str(args.get("reason", ""))[:200]})
            return {"status": new}

        def schedule_appointment(c: AgentContext, args: dict) -> dict:
            lead = lead_of(c)
            try:
                start = datetime.fromisoformat(str(args["starts_at"]))
                end = datetime.fromisoformat(str(args["ends_at"]))
            except (KeyError, ValueError) as exc:
                raise ToolError("starts_at and ends_at must be ISO-8601 with timezone offset") from exc
            if start.tzinfo is None or end.tzinfo is None or end <= start or start < utcnow():
                raise ToolError("times must be timezone-aware, in the future, and end after start")
            cal = registry.calendar()
            if any(b0 < end and start < b1 for b0, b1 in cal.busy_periods(start, end)):
                raise ToolError("that slot conflicts with an existing calendar event")
            title = str(args.get("title") or f"Seller call: lead #{lead.id}")[:200]
            event_id = cal.create_event(title, start, end, args.get("location"), f"Jarvis lead #{lead.id}")
            appt = Appointment(lead_id=lead.id, title=title, starts_at=start, ends_at=end, location=args.get("location"),
                               gcal_event_id=event_id)
            c.session.add(appt)
            lead.status = LeadStatus.APPOINTMENT_SET
            c.session.flush()
            return {"appointment_id": appt.id, "note": "internal calendar hold created; the seller was NOT invited (that needs an approved message)"}

        obj = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}  # noqa: E731
        txt = {"type": "string"}
        return [
            Tool("get_lead_context", "Read lead facts, known/missing discovery topics, and recent conversation.", obj({}), get_context),
            Tool("record_discovery", "Record discovery answers the seller actually stated (omit unknowns).",
                 obj({k: txt for k in DISCOVERY_TOPICS}), record_discovery),
            Tool("draft_reply", "Draft an SMS or email reply. It is queued for human approval and is NOT sent.",
                 obj({"channel": {"type": "string", "enum": ["sms", "email"]}, "body": txt, "subject": txt}, ["channel", "body"]), draft_reply),
            Tool("save_summary", "Save a factual conversation summary and push it to the CRM.",
                 obj({"summary": txt, "channel": txt}, ["summary"]), save_summary),
            Tool("update_lead_status", "Set lead status to engaged, nurture, dead, or do_not_contact.",
                 obj({"status": {"type": "string", "enum": sorted(x.value for x in SETTABLE_STATUSES)}, "reason": txt}, ["status"]), update_status),
            Tool("schedule_appointment", "Create an internal calendar hold after the seller has agreed to a time. Does not invite the seller.",
                 obj({"starts_at": txt, "ends_at": txt, "title": txt, "location": txt}, ["starts_at", "ends_at"]), schedule_appointment),
        ]

    def handle_lead(self, session: Session, lead_id: int, instruction: str, triggered_by: str = "system"):
        data = {"lead_id": lead_id}
        run = self.run(session, instruction, triggered_by=triggered_by, data=data)
        if not data.get("summarized") and not run.stopped_early:
            self.run(session, "Call save_summary now with a factual summary of this conversation so far. Do nothing else.",
                     triggered_by=triggered_by, data=data)
        return run, data

    def start_outreach(self, session: Session, lead_id: int, triggered_by: str = "system"):
        return self.handle_lead(session, lead_id, "This lead has not been contacted yet. Draft a friendly first message "
                                "introducing yourself as an automated assistant for the team and asking whether they are open to selling and how soon.", triggered_by)

    def handle_reply(self, session: Session, lead_id: int, triggered_by: str = "system"):
        return self.handle_lead(session, lead_id, "The seller just replied. Review the conversation, record discovery answers, "
                                "and draft the next reply for approval.", triggered_by)
