from datetime import UTC, datetime, timedelta

import pytest

from bravo import approvals
from bravo.agents.disposition import BuyerDispositionAgent
from bravo.agents.followup import FollowUpAgent
from bravo.agents.testing import ScriptedLLM, text, tool_use
from bravo.enums import ApprovalStatus, LeadStatus, MessageStatus, Role
from bravo.integrations import registry
from bravo.integrations.base import SendResult
from bravo.models import ActivityLog, Appointment, Approval, BuyerInteraction, DealAnalysis, Message, Task
from bravo.services import buyers, followup, inbound, leads, optout, outbound
from bravo.services.deal_math import compute_mao

TUE_NOON = datetime(2026, 3, 3, 18, 0, tzinfo=UTC)  # noon Chicago
TUE_NIGHT = datetime(2026, 3, 4, 3, 0, tzinfo=UTC)


class Rec:
    def __init__(self):
        self.sms, self.email = [], []

    def send(self, to, *a):
        (self.sms if len(a) == 1 else self.email).append((to, *a))
        return SendResult(f"id{len(self.sms) + len(self.email)}")


@pytest.fixture
def rec(monkeypatch):
    r = Rec()
    registry.configure(sms=r, email=r)
    monkeypatch.setattr(outbound, "utcnow", lambda: TUE_NOON)
    return r


def mk_lead(session, phone="512-867-5309", street="12 Oak Lane", **kw):
    r = leads.ingest_lead(session, {"name": "Jane Doe", "phone": phone, "email": f"jane{phone[-2:]}@example.com", "street": street, "city": "Austin",
                                    "state": "TX", "zip": "78701", **kw}, source="web_form")
    return r.lead


# ---- follow-up ----

def test_scan_creates_status_based_tasks_without_duplicates(session):
    a = mk_lead(session, street="1 A St")
    b = mk_lead(session, phone="512-867-5310", street="2 B St")
    b.status = LeadStatus.NURTURE
    c = mk_lead(session, phone="512-867-5311", street="3 C St")
    c.status = LeadStatus.DO_NOT_CONTACT
    session.query(Task).delete()
    created = followup.scan_followups(session, now=TUE_NOON)
    kinds = {t.lead_id: t.kind for t in created}
    assert kinds[a.id] == "contact" and kinds[b.id] == "follow_up" and c.id not in kinds
    assert followup.scan_followups(session, now=TUE_NOON) == []
    nurture = next(t for t in created if t.lead_id == b.id)
    assert nurture.due_at > TUE_NOON + timedelta(days=29)


def test_task_due_dates_land_inside_contact_hours(session):
    from bravo.services.hours import is_contact_allowed

    mk_lead(session)
    session.query(Task).delete()
    t = followup.scan_followups(session, now=TUE_NIGHT)[0]
    assert is_contact_allowed(t.due_at, "TX") and t.due_at > TUE_NIGHT


def test_contacted_sequence_and_nurture_decision(session, rec):
    lead = mk_lead(session)
    lead.status = LeadStatus.CONTACTED
    session.query(Task).delete()
    plan = followup.decide_followup(session, lead, TUE_NOON)
    assert plan[0] == "follow_up"
    conv = outbound.get_conversation(session, lead.id, "sms")
    for i in range(5):
        session.add(Message(conversation_id=conv.id, direction="outbound", channel="sms", body="x", status="sent", sent_at=TUE_NOON - timedelta(days=30 - i)))
    session.flush()
    assert followup.decide_followup(session, lead, TUE_NOON)[0] == "nurture_decision"


def test_appointment_reminder_and_offer_followup(session):
    lead = mk_lead(session)
    lead.status = LeadStatus.APPOINTMENT_SET
    session.add(Appointment(lead_id=lead.id, title="t", starts_at=TUE_NOON + timedelta(days=3), ends_at=TUE_NOON + timedelta(days=3, hours=1)))
    session.flush()
    kind, _, due = followup.decide_followup(session, lead, TUE_NOON)
    assert kind == "appointment_reminder" and due == TUE_NOON + timedelta(days=2)
    lead.status = LeadStatus.OFFER_SENT
    assert followup.decide_followup(session, lead, TUE_NOON)[0] == "offer_follow_up"


def test_hot_lead_escalation_once_per_day(session):
    hot = mk_lead(session, timeline="asap", reason="foreclosure", condition="needs roof", price_expectation="open to offers")
    assert hot.motivation_score >= 70
    first = followup.escalate_hot_leads(session, now=TUE_NOON)
    assert len(first) == 1 and first[0].priority == 1 and "HOT" in first[0].title
    assert followup.escalate_hot_leads(session, now=TUE_NOON + timedelta(hours=1)) == []
    assert len(followup.escalate_hot_leads(session, now=TUE_NOON + timedelta(hours=25))) == 1
    assert any(e.event == "lead.escalated" for e in session.query(ActivityLog))


def test_reminders_fire_once(session):
    session.add(Task(title="call", due_at=TUE_NOON - timedelta(hours=1), priority=2))
    session.add(Task(title="later", due_at=TUE_NOON + timedelta(days=1)))
    session.flush()
    assert [t.title for t in followup.due_reminders(session, TUE_NOON)] == ["call"]
    assert followup.due_reminders(session, TUE_NOON) == []


def test_campaign_requires_approval_and_filters_recipients(session, rec):
    ok = mk_lead(session, street="1 A St")
    dnc = mk_lead(session, phone="512-867-5310", street="2 B St")
    optout.record_opt_out(session, "sms", "+15128675310")
    noph = leads.ingest_lead(session, {"email": "x@y.com", "street": "3 C St", "zip": "78701"}, source="csv").lead
    appr = followup.propose_campaign(session, name="Spring", lead_ids=[ok.id, dnc.id, noph.id, 9999], template="Hi {first_name}, still thinking about selling {street}?",
                                     channel="sms", requested_by="agent:followup")
    assert appr.payload["skipped"] == {"inactive_or_dnc": 1, "no_contact": 1, "missing": 1}
    assert len(appr.payload["items"]) == 1 and "Jane" in appr.payload["items"][0]["body"] and rec.sms == []
    approvals.decide(session, appr.id, approve=True, user_label="user:2", user_role=Role.MANAGER)
    assert len(rec.sms) == 1 and appr.status == ApprovalStatus.EXECUTED
    again = None
    with pytest.raises(followup.CampaignError):
        again = followup.propose_campaign(session, name="Again", lead_ids=[ok.id], template="Hi {first_name}", channel="sms", requested_by="a", now=TUE_NOON)
    assert again is None


def test_campaign_outside_hours_is_scheduled_and_rejection_cancels(session, rec, monkeypatch):
    monkeypatch.setattr(outbound, "utcnow", lambda: TUE_NIGHT)
    lead = mk_lead(session)
    a = followup.propose_campaign(session, name="N", lead_ids=[lead.id], template="Hi {first_name}, checking in on {street}.", channel="sms", requested_by="agent:followup")
    approvals.decide(session, a.id, approve=True, user_label="u", user_role=Role.ADMIN)
    msg = session.get(Message, a.payload["items"][0]["message_id"])
    assert msg.status == MessageStatus.SCHEDULED and rec.sms == []
    lead2 = mk_lead(session, phone="512-867-5310", street="9 Z St")
    b = followup.propose_campaign(session, name="N2", lead_ids=[lead2.id], template="Hi {first_name}, hello.", channel="sms", requested_by="a")
    approvals.decide(session, b.id, approve=False, user_label="u", user_role=Role.ADMIN)
    assert session.get(Message, b.payload["items"][0]["message_id"]).status == MessageStatus.REJECTED


def test_campaign_template_validation():
    with pytest.raises(followup.CampaignError):
        followup.validate_template("Hi {seller_phone}", "sms")
    with pytest.raises(followup.CampaignError):
        followup.validate_template("Guaranteed $50k for your {street}", "sms")


def test_followup_agent_tools_and_cycle(session, rec):
    lead = mk_lead(session)
    llm = ScriptedLLM([tool_use("run_followup_scan", {}), tool_use("escalate_hot_leads", {}, "t2"),
                       tool_use("propose_campaign", {"name": "x", "lead_ids": [lead.id], "template": "We guarantee it", "channel": "sms"}, "t3")],
                      [text("done")])
    run = FollowUpAgent(llm).run(session, "run the daily cycle")
    assert [c["ok"] for c in run.tool_calls] == [True, True, False] and "compliance" in run.tool_calls[2]["error"]
    assert set(FollowUpAgent().run_cycle(session)) == {"tasks_created", "escalated", "reminders"}


# ---- buyers / disposition ----

def mk_analysis(session, lead):
    r = compute_mao(address="12 Oak Lane", arv=200000, repair_estimate=30000, closing_costs=4000, assignment_fee=10000)
    row = DealAnalysis(lead_id=lead.id, address="12 Oak Lane", inputs=r.to_dict()["inputs"], results=r.to_dict(), assumptions=r.assumptions)
    session.add(row)
    session.flush()
    return row


def mk_buyer(session, name="Bo Buyer", email="bo@cash.com", phone="415-867-5309", **kw):
    crit = kw.pop("criteria", {"states": ["TX"], "max_price": 150000})
    return buyers.upsert_buyer(session, name=name, email=email, phone=phone, criteria=crit, approved=kw.get("approved", True),
                               proof_of_funds_verified=kw.get("pof", True))


def test_matching_only_approved_verified_and_never_guesses(session):
    lead = mk_lead(session, timeline="asap", reason="divorce")
    lead.property.beds = None
    good = mk_buyer(session)
    mk_buyer(session, name="Unapproved", email="u@x.com", approved=False)
    mk_buyer(session, name="NoPOF", email="n@x.com", pof=False)
    mk_buyer(session, name="WrongState", email="w@x.com", criteria={"states": ["FL"]})
    mk_buyer(session, name="Budget", email="b@x.com", criteria={"states": ["TX"], "max_price": 50000})
    mk_buyer(session, name="Beds", email="beds@x.com", criteria={"states": ["TX"], "min_beds": 3})
    analysis = mk_analysis(session, lead)
    price = buyers.asking_price_of(analysis)
    assert float(price) == 96000 + 10000
    ms = buyers.match_buyers(session, lead, price)
    assert [m.buyer.id for m in ms] == [good.id]
    with_unknown = buyers.match_buyers(session, lead, price, include_unknown=True)
    assert {m.buyer.name for m in with_unknown} == {"Bo Buyer", "Beds"}
    assert any("beds unknown" in u for m in with_unknown for u in m.unknowns)


def test_buyer_validation():
    with pytest.raises(buyers.BuyerError):
        buyers.clean_criteria({"secret": 1})


def test_deal_summary_never_leaks_seller_info(session):
    lead = mk_lead(session, timeline="within a month", reason="facing foreclosure on the property", mortgage="behind on payments 3 months")
    analysis = mk_analysis(session, lead)
    subject, body = buyers.build_deal_summary(lead, analysis, "Bo Buyer")
    assert "Austin" in body and "$106,000" in body and "Oak" not in body and "unverified" in body
    assert "guarantee of value" in body and buyers.leaks_seller_info(lead, body) == []
    leaky = body + " Seller Jane Doe, 512-867-5309, jane09@example.com, facing foreclosure on the property, 12 Oak Lane"
    assert set(buyers.leaks_seller_info(lead, leaky)) >= {"seller name", "seller phone", "seller email", "seller reason", "street address"}
    _, revealed = buyers.build_deal_summary(lead, analysis, "Bo Buyer", reveal_street=True)
    assert "12 OAK LN" in revealed


def test_blast_needs_approval_then_sends_to_buyers_only(session, rec):
    lead = mk_lead(session)
    analysis = mk_analysis(session, lead)
    b1, b2 = mk_buyer(session), mk_buyer(session, name="Cy Cash", email="cy@cash.com", phone="415-867-5310")
    optout.record_opt_out(session, "sms", "+14158675310")
    appr = buyers.propose_blast(session, lead_id=lead.id, analysis_id=analysis.id, buyer_ids=[b1.id, b2.id, 999], channel="email", requested_by="agent:buyer_disposition")
    assert len(appr.payload["items"]) == 2 and rec.email == []
    approvals.decide(session, appr.id, approve=True, user_label="user:2", user_role=Role.MANAGER)
    assert sorted(t[0] for t in rec.email) == ["bo@cash.com", "cy@cash.com"] and rec.sms == []
    assert all("Jane" not in t[2] and "jane09@example.com" not in t[2] for t in rec.email)
    assert {i.status for i in session.query(BuyerInteraction)} == {"sent"}
    assert lead.last_contacted_at is None and lead.status == LeadStatus.NEW  # buyer outreach is not seller contact
    with pytest.raises(buyers.BuyerError):
        buyers.propose_blast(session, lead_id=lead.id, analysis_id=analysis.id, buyer_ids=[b1.id], channel="email", requested_by="a")  # already contacted


def test_blast_blocked_for_unviable_deal_and_buyer_optout_blocks_pending(session, rec):
    lead = mk_lead(session)
    bad = compute_mao(address="x", arv=100000, repair_estimate=80000, closing_costs=4000, assignment_fee=10000)
    row = DealAnalysis(lead_id=lead.id, address="x", inputs=bad.to_dict()["inputs"], results=bad.to_dict(), assumptions=[])
    session.add(row)
    session.flush()
    b = mk_buyer(session)
    with pytest.raises(buyers.BuyerError, match="not viable"):
        buyers.propose_blast(session, lead_id=lead.id, analysis_id=row.id, buyer_ids=[b.id], channel="sms", requested_by="a")
    good = mk_analysis(session, lead)
    a = buyers.propose_blast(session, lead_id=lead.id, analysis_id=good.id, buyer_ids=[b.id], channel="sms", requested_by="a")
    inbound.handle_inbound_sms(session, "+14158675309", "STOP")
    assert session.get(Message, a.payload["items"][0]["message_id"]).status == MessageStatus.BLOCKED
    approvals.decide(session, a.id, approve=True, user_label="u", user_role=Role.ADMIN)
    assert rec.sms == [] and a.status == ApprovalStatus.FAILED


def test_buyer_reply_routing_and_agent_classification(session, rec):
    lead = mk_lead(session)
    analysis = mk_analysis(session, lead)
    b = mk_buyer(session)
    a = buyers.propose_blast(session, lead_id=lead.id, analysis_id=analysis.id, buyer_ids=[b.id], channel="sms", requested_by="a")
    approvals.decide(session, a.id, approve=True, user_label="u", user_role=Role.ADMIN)
    r = inbound.handle_inbound_sms(session, "+14158675309", "Interested! Can I see it Thursday?", "SMb1")
    assert r.action == "buyer_message" and r.lead_id == lead.id
    llm = ScriptedLLM([tool_use("record_buyer_response", {"buyer_id": b.id, "status": "interested", "quote": "I will pay $500k cash"}),
                       tool_use("record_buyer_response", {"buyer_id": b.id, "status": "interested", "quote": "Interested! Can I see it Thursday?"}, "t2")],
                      [text("recorded")])
    run = BuyerDispositionAgent(llm).handle_buyer_reply(session, lead.id, b.id, "Interested! Can I see it Thursday?")
    assert [c["ok"] for c in run.tool_calls] == [False, True]
    assert session.query(BuyerInteraction).one().status == "interested"
    assert session.query(Task).filter(Task.kind == "buyer_response").count() >= 1
    # replies to seller threads never see buyer messages
    assert session.query(Message).filter(Message.buyer_id.is_(None), Message.direction == "inbound").count() == 0


def test_cannot_record_response_without_outreach(session):
    lead = mk_lead(session)
    b = mk_buyer(session)
    with pytest.raises(buyers.BuyerError, match="no outreach"):
        buyers.record_response(session, lead_id=lead.id, buyer_id=b.id, status="interested", note="x", actor="u")


def test_disposition_agent_end_to_end(session, rec):
    lead = mk_lead(session)
    analysis = mk_analysis(session, lead)
    b = mk_buyer(session)
    llm = ScriptedLLM([tool_use("find_matching_buyers", {})],
                      [tool_use("propose_buyer_blast", {"buyer_ids": [b.id, 12345], "channel": "email", "reveal_street": True}, "t2")],
                      [text("queued")])
    run = BuyerDispositionAgent(llm).disposition(session, lead.id, analysis.id)
    assert run.tool_calls[0]["result"]["matches"][0]["buyer_id"] == b.id
    appr = session.get(Approval, run.tool_calls[1]["result"]["approval_id"])
    assert "OAK" not in appr.payload["items"][0]["body"].upper()  # agent may not reveal the street unless the caller allowed it
    assert rec.email == []
