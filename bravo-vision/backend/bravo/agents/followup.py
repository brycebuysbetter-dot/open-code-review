"""Follow-up Agent. Rules and timing are deterministic; the model can only invoke these guarded tools."""
from sqlalchemy.orm import Session

from bravo.agents.base import Agent, AgentContext, Tool, ToolError
from bravo.services import followup


class FollowUpAgent(Agent):
    name = "followup"
    role_prompt = """\
Role: Follow-up. Keep no lead forgotten. Use run_followup_scan to create follow-up tasks from lead status, escalate_hot_leads for
high-motivation leads, and list_due_reminders to see what is due. To re-engage a group, call propose_campaign with a short template
using only {first_name} and {street}; it is queued for human approval and nothing is sent until approved. Contact only happens
inside approved business hours; the system enforces that. Report briefly what you did."""

    def tools(self, ctx: AgentContext) -> list[Tool]:
        def scan(c, a):
            tasks = followup.scan_followups(c.session)
            return {"created": [{"task_id": t.id, "lead_id": t.lead_id, "kind": t.kind, "title": t.title} for t in tasks]}

        def escalate(c, a):
            tasks = followup.escalate_hot_leads(c.session)
            return {"escalated": [{"task_id": t.id, "lead_id": t.lead_id} for t in tasks]}

        def reminders(c, a):
            return {"due": [{"task_id": t.id, "lead_id": t.lead_id, "title": t.title} for t in followup.due_reminders(c.session)]}

        def campaign(c, a):
            try:
                appr = followup.propose_campaign(c.session, name=str(a.get("name", "Campaign"))[:100], lead_ids=list(a.get("lead_ids", [])),
                                                 template=str(a.get("template", "")), channel=str(a.get("channel", "sms")),
                                                 subject=a.get("subject"), requested_by=c.actor)
            except followup.CampaignError as exc:
                raise ToolError(str(exc)) from exc
            return {"approval_id": appr.id, "summary": appr.summary, "note": "awaiting human approval; nothing sent"}

        empty = {"type": "object", "properties": {}}
        return [
            Tool("run_followup_scan", "Create follow-up tasks for leads based on status and timing.", empty, scan),
            Tool("escalate_hot_leads", "Escalate leads at or above the hot threshold to priority-1 tasks.", empty, escalate),
            Tool("list_due_reminders", "List tasks that are newly due.", empty, reminders),
            Tool("propose_campaign", "Queue a message campaign for human approval.", {
                "type": "object", "required": ["name", "lead_ids", "template", "channel"],
                "properties": {"name": {"type": "string"}, "lead_ids": {"type": "array", "items": {"type": "integer"}},
                               "template": {"type": "string"}, "channel": {"type": "string", "enum": ["sms", "email"]},
                               "subject": {"type": "string"}}}, campaign),
        ]

    def run_cycle(self, session: Session) -> dict:
        """Deterministic cron entry point: no LLM needed."""
        created = followup.scan_followups(session)
        escalated = followup.escalate_hot_leads(session)
        reminders = followup.due_reminders(session)
        return {"tasks_created": len(created), "escalated": len(escalated), "reminders": len(reminders)}

