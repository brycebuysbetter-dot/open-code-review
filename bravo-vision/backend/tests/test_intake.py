from bravo.agents import intake
from bravo.agents.base import Agent
from bravo.agents.llm import LLMUnavailable
from bravo.agents.testing import ScriptedLLM, text, tool_use
from bravo.config import get_settings
from bravo.enums import LeadStatus
from bravo.integrations import registry
from bravo.integrations.base import InboundEmail
from bravo.integrations.crm.local import LocalCRM
from bravo.models import ActivityLog, Lead, Property, Seller, Task
from bravo.services import leads

CSV = """Owner Name,Phone Number,Property Address,City,State,Zip,Notes
Jane Doe,(512) 867-5309,12 Oak Lane,Austin,TX,78701,"facing foreclosure, need to sell in 3 weeks"
Jane Doe,512.867.5309,12 OAK LN,Austin,Texas,78701-0001,second submission
Bob,not-a-phone,,,,,
,,,,,,
Sam Roe,+1 415 555 2671,"9 Elm Street, Dallas, TX 75001",,,,no rush just curious
"""


def test_csv_import_normalizes_dedupes_scores(session):
    res = intake.import_csv(session, CSV.encode())
    session.commit()
    assert (res.created, res.duplicates, res.rejected) == (2, 1, 1)
    jane = session.query(Lead).join(Property).filter(Property.street == "12 OAK LN").one()
    assert jane.motivation_score >= 40 and jane.seller.phone == "+15128675309" and jane.property.zip == "78701"
    assert len(jane.raw_data["duplicate_submissions"]) == 1
    sam = session.query(Lead).join(Property).filter(Property.city == "Dallas").one()
    assert sam.motivation_score < 20 and sam.priority == 4
    assert session.query(Seller).count() == 2


def test_same_seller_new_property_is_new_lead_shared_seller(session):
    leads.ingest_lead(session, {"phone": "512-867-5309", "street": "1 A St", "zip": "78701"}, source="csv")
    r = leads.ingest_lead(session, {"phone": "512-867-5309", "street": "2 B St", "zip": "78701"}, source="csv")
    assert r.created and session.query(Seller).count() == 1 and session.query(Lead).count() == 2


def test_addressless_leads_dedupe_by_contact(session):
    a = leads.ingest_lead(session, {"phone": "512-867-5309"}, source="sms_reply")
    b = leads.ingest_lead(session, {"phone": "(512) 867-5309", "message": "call me"}, source="sms_reply")
    assert a.created and b.duplicate_of.id == a.lead.id and "missing property address" in a.issues


def test_unusable_record_rejected(session):
    r = leads.ingest_lead(session, {"name": "Only A Name"}, source="csv")
    assert r.lead is None and r.issues


def test_hot_lead_gets_priority_one_task(session):
    r = leads.ingest_lead(session, {"phone": "512-867-5309", "street": "1 A St", "zip": "78701", "timeline": "asap",
                                    "reason": "foreclosure", "condition": "needs roof", "price_expectation": "open to offers"},
                          source="web_form")
    assert r.lead.priority == 1
    t = session.query(Task).one()
    assert t.kind == "hot_lead" and t.lead_id == r.lead.id


def test_web_form_aliases(session):
    r = intake.import_web_form(session, {"Full Name": "A B", "Cell": "512-867-5309", "Street Address": "5 Pine Rd",
                                         "Zip Code": "78701", "Reason for Selling": "divorce"})
    assert r.created and r.lead.discovery["reason"] == "divorce"


def test_crm_import(session):
    class C(LocalCRM):
        def fetch_leads(self, limit=100):
            return [{"name": "X", "phone": "512-867-5309", "street": "7 Cedar Dr", "zip": "78701", "crm_id": "hs1"}]

    registry.configure(crm=C())
    res = intake.import_from_crm(session)
    assert res.created == 1 and session.query(Lead).one().crm_id == "hs1"


EMAIL = InboundEmail("m1", "Jane Doe <jane@example.com>", "Selling my house",
                     "Hi, I need to sell my house at 12 Oak Lane, Austin TX 78701 within a month, divorce. Call 512-867-5309.")


def test_email_agent_extracts_and_drops_hallucinations(session):
    llm = ScriptedLLM(
        [tool_use("submit_lead", {"name": "Jane Doe", "phone": "512-867-5309", "email": "invented@fake.com",
                                  "street": "12 Oak Lane", "city": "Austin", "state": "TX", "zip": "78701",
                                  "timeline": "within a month", "reason": "divorce"})],
        [text("Lead created.")],
    )
    res = intake.LeadIntakeAgent(llm).process_email(session, EMAIL)
    assert res.created and res.lead.seller.email is None  # invented email dropped
    assert res.lead.seller.phone == "+15128675309" and res.lead.motivation_score >= 40
    events = {e.event for e in session.query(ActivityLog)}
    assert {"agent.run_started", "agent.tool_call", "agent.tool_result", "intake.ungrounded_fields_dropped", "lead.ingested", "crm.synced"} <= events
    tool_result_msg = llm.calls[1]["messages"][-1]["content"][0]
    assert tool_result_msg["type"] == "tool_result" and "invented" not in tool_result_msg["content"]


def test_email_agent_dismisses_spam(session):
    llm = ScriptedLLM([tool_use("dismiss_email", {"reason": "SEO vendor"})], [text("Dismissed.")])
    spam = InboundEmail("m2", "x@y.com", "Rank #1 on Google", "buy our seo")
    assert intake.LeadIntakeAgent(llm).process_email(session, spam) is None
    assert session.query(Lead).count() == 0


def test_email_agent_falls_back_without_llm(session):
    class Down:
        def create(self, **kw):
            raise LLMUnavailable("no key")

    res = intake.LeadIntakeAgent(Down()).process_email(session, EMAIL)
    assert res.created and res.lead.seller.email == "jane@example.com" and res.lead.property.street == "12 OAK LN"


def test_tool_failure_is_isolated_and_reported(session):
    llm = ScriptedLLM([tool_use("submit_lead", {"phone": "999-999-9999"})], [text("Could not.")])
    intake.LeadIntakeAgent(llm).process_email(session, EMAIL)
    result_block = llm.calls[1]["messages"][-1]["content"][0]
    assert result_block["is_error"] and "no verifiable" in result_block["content"]
    assert session.query(Lead).count() == 0


def test_unknown_tool_and_step_limit(session, monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_max_steps", 2)
    llm = ScriptedLLM([tool_use("rm_rf", {})], [tool_use("rm_rf", {}, "tu_2")])
    run = Agent(llm).run(session, "go")
    assert run.stopped_early and run.tool_calls[0]["error"].startswith("unknown tool")


def test_poll_gmail(session):
    class G:
        def fetch_inbound(self, q, n):
            return [EMAIL]

    registry.configure(email=G())
    llm = ScriptedLLM([tool_use("submit_lead", {"phone": "512-867-5309", "street": "12 Oak Lane", "zip": "78701"})], [text("ok")])
    out = intake.LeadIntakeAgent(llm).poll_gmail(session)
    assert out.created == 1


def test_opted_out_number_imports_as_dnc(session):
    from bravo.services import optout

    optout.record_opt_out(session, "sms", "+15128675309")
    r = leads.ingest_lead(session, {"phone": "512-867-5309", "street": "1 A St", "zip": "78701", "timeline": "asap"}, source="csv")
    assert r.lead.status == LeadStatus.DO_NOT_CONTACT and r.lead.priority == 4
