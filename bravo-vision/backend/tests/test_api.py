from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from bravo import db
from bravo.agents.llm import set_llm
from bravo.agents.testing import ScriptedLLM, text, tool_use
from bravo.api import deps
from bravo.config import get_settings
from bravo.integrations import registry
from bravo.integrations.base import SendResult
from bravo.main import create_app
from bravo.models import ActivityLog, User
from bravo.services import outbound

TUE_NOON = datetime(2026, 3, 3, 18, 0, tzinfo=UTC)


@pytest.fixture
def api(monkeypatch):
    db.init_engine("sqlite://")
    db.create_all()
    tokens = {}
    with db.session_scope() as s:
        for role in ("admin", "manager", "acquisitions", "dispositions", "viewer"):
            t = deps.new_token()
            s.add(User(email=f"{role}@x.com", role=role, token_hash=deps.hash_token(t)))
            tokens[role] = t
    monkeypatch.setattr(outbound, "utcnow", lambda: TUE_NOON)
    with TestClient(create_app()) as client:
        client.tokens = tokens
        client.h = lambda role: {"Authorization": f"Bearer {tokens[role]}"}
        yield client
    set_llm(None)
    db.Base.metadata.drop_all(db.get_engine())


LEAD = {"name": "Jane Doe", "phone": "512-867-5309", "email": "jane@example.com", "street": "12 Oak Lane", "city": "Austin",
        "state": "TX", "zip": "78701", "timeline": "asap", "reason": "foreclosure"}


def test_auth_required_and_rbac(api):
    assert api.get("/api/leads").status_code == 401
    assert api.get("/api/leads", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert api.get("/api/leads", headers=api.h("viewer")).status_code == 200
    assert api.post("/api/leads", json=LEAD, headers=api.h("viewer")).status_code == 403
    assert api.post("/api/users", json={"email": "a@b.com", "role": "viewer"}, headers=api.h("manager")).status_code == 403
    r = api.post("/api/users", json={"email": "new@b.com", "role": "viewer"}, headers=api.h("admin"))
    assert r.status_code == 200 and r.json()["token"].startswith("bv_")
    assert api.get("/api/me", headers={"Authorization": f"Bearer {r.json()['token']}"}).json()["role"] == "viewer"
    assert api.post("/api/users", json={"email": "new@b.com", "role": "viewer"}, headers=api.h("admin")).status_code == 409


def test_viewer_sees_masked_pii(api):
    lid = api.post("/api/leads", json=LEAD, headers=api.h("acquisitions")).json()["lead"]["id"]
    full = api.get(f"/api/leads/{lid}", headers=api.h("acquisitions")).json()["seller"]
    masked = api.get(f"/api/leads/{lid}", headers=api.h("viewer")).json()["seller"]
    assert full["phone"] == "+15128675309" and masked["phone"] == "***5309" and masked["email"] == "j***@example.com"


def test_lead_crud_dedupe_and_csv(api):
    r = api.post("/api/leads", json=LEAD, headers=api.h("acquisitions")).json()
    assert r["created"] and r["lead"]["motivation_score"] > 0
    dup = api.post("/api/leads", json={**LEAD, "notes": "again"}, headers=api.h("acquisitions")).json()
    assert not dup["created"] and dup["duplicate_of"] == r["lead"]["id"]
    assert api.post("/api/leads", json={"name": "x"}, headers=api.h("acquisitions")).status_code == 422
    csv = b"name,phone,address\nBob,415-867-5309,\"9 Elm St, Dallas, TX 75001\"\n"
    out = api.post("/api/leads/import-csv", files={"file": ("l.csv", csv, "text/csv")}, headers=api.h("acquisitions")).json()
    assert out["created"] == 1
    assert len(api.get("/api/leads?min_score=1", headers=api.h("viewer")).json()) >= 1
    assert api.get("/api/leads?q=elm", headers=api.h("viewer")).json()[0]["property"]["street"].startswith("9 ELM")
    assert api.get("/api/leads/9999", headers=api.h("viewer")).status_code == 404


def test_message_approval_flow_over_http(api):
    sent = []
    registry.configure(sms=type("S", (), {"send": lambda self, to, body: sent.append(body) or SendResult("x")})())
    lid = api.post("/api/leads", json=LEAD, headers=api.h("acquisitions")).json()["lead"]["id"]
    bad = api.post(f"/api/leads/{lid}/messages/draft", json={"channel": "sms", "body": "We guarantee $5"}, headers=api.h("acquisitions"))
    assert bad.status_code == 422 and bad.json()["detail"]["violations"]
    d = api.post(f"/api/leads/{lid}/messages/draft", json={"channel": "sms", "body": "Hi Jane, are you still open to selling?"}, headers=api.h("acquisitions"))
    assert d.status_code == 200 and sent == []
    pending = api.get("/api/approvals", headers=api.h("viewer")).json()
    assert len(pending) == 1 and pending[0]["kind"] == "message" and "Reply STOP" in pending[0]["payload"]["body"]
    aid = pending[0]["id"]
    assert api.post(f"/api/approvals/{aid}/decision", json={"approve": True}, headers=api.h("viewer")).status_code == 403
    assert api.post(f"/api/approvals/{aid}/decision", json={"approve": True}, headers=api.h("dispositions")).status_code == 403
    ok = api.post(f"/api/approvals/{aid}/decision", json={"approve": True, "note": "ok"}, headers=api.h("manager"))
    assert ok.status_code == 200 and ok.json()["status"] == "executed" and len(sent) == 1
    assert api.post(f"/api/approvals/{aid}/decision", json={"approve": True}, headers=api.h("manager")).status_code == 409
    with db.session_scope() as s:
        events = {e.event for e in s.query(ActivityLog)}
    assert {"approval.requested", "approval.approved", "approval.executed", "message.sent", "approval.denied"} <= events


def test_deal_endpoints_and_offer_approval(api):
    lid = api.post("/api/leads", json=LEAD, headers=api.h("acquisitions")).json()["lead"]["id"]
    body = {"address": "12 Oak Lane", "arv": "200000", "repair_estimate": "30000", "closing_costs": "4000", "assignment_fee": "10000", "lead_id": lid}
    r = api.post("/api/deals/analyze", json=body, headers=api.h("acquisitions"))
    assert r.status_code == 200 and r.json()["result"]["mao"] == "96000.00" and "Assumptions" in r.json()["summary"]
    aid = r.json()["analysis_id"]
    assert api.post("/api/deals/analyze", json={**body, "arv": "0"}, headers=api.h("acquisitions")).status_code == 422
    assert api.get(f"/api/deals/{aid}", headers=api.h("viewer")).status_code == 403  # viewers cannot run deal tools
    over = api.post(f"/api/deals/{aid}/offer", json={"lead_id": lid, "amount": "120000"}, headers=api.h("acquisitions"))
    assert over.status_code == 422 and "exceeds" in over.json()["detail"]
    off = api.post(f"/api/deals/{aid}/offer", json={"lead_id": lid, "amount": "90000"}, headers=api.h("acquisitions")).json()
    assert api.post(f"/api/approvals/{off['approval_id']}/decision", json={"approve": True}, headers=api.h("acquisitions")).status_code == 403
    assert api.post(f"/api/approvals/{off['approval_id']}/decision", json={"approve": True}, headers=api.h("manager")).json()["status"] == "executed"


def test_contract_payment_and_destructive_need_admin(api):
    lid = api.post("/api/leads", json=LEAD, headers=api.h("acquisitions")).json()["lead"]["id"]
    c = api.post("/api/contracts", json={"lead_id": lid, "kind": "purchase", "doc_ref": "drive://x"}, headers=api.h("acquisitions"))
    assert c.status_code == 403  # acquisitions cannot create contracts
    c = api.post("/api/contracts", json={"lead_id": lid, "kind": "purchase", "doc_ref": "drive://x"}, headers=api.h("manager")).json()
    assert api.post(f"/api/approvals/{c['approval_id']}/decision", json={"approve": True}, headers=api.h("manager")).status_code == 403
    assert api.post(f"/api/approvals/{c['approval_id']}/decision", json={"approve": True}, headers=api.h("admin")).status_code == 200
    p = api.post("/api/payments/request", json={"amount": "500", "payee": "Title", "purpose": "earnest"}, headers=api.h("manager")).json()
    assert api.post(f"/api/approvals/{p['id']}/decision", json={"approve": True}, headers=api.h("admin")).json()["result"]["recorded"]
    d = api.post(f"/api/leads/{lid}/destructive-request", json={"action": "purge_lead", "reason": "deletion request"}, headers=api.h("acquisitions")).json()
    assert api.post(f"/api/approvals/{d['id']}/decision", json={"approve": True}, headers=api.h("manager")).status_code == 403
    assert api.post(f"/api/approvals/{d['id']}/decision", json={"approve": True}, headers=api.h("admin")).json()["status"] == "executed"
    assert api.get(f"/api/leads/{lid}", headers=api.h("admin")).status_code == 404


def test_buyers_and_blast_over_http(api):
    registry.configure(email=type("E", (), {"send": lambda self, to, s, b: SendResult("e")})())
    lid = api.post("/api/leads", json=LEAD, headers=api.h("acquisitions")).json()["lead"]["id"]
    aid = api.post("/api/deals/analyze", json={"address": "x", "arv": 200000, "repair_estimate": 30000, "closing_costs": 4000, "assignment_fee": 10000, "lead_id": lid},
                   headers=api.h("acquisitions")).json()["analysis_id"]
    b = api.post("/api/buyers", json={"name": "Bo", "email": "bo@cash.com", "criteria": {"states": ["TX"]}, "approved": True, "proof_of_funds_verified": True},
                 headers=api.h("dispositions"))
    assert b.status_code == 200
    assert api.post("/api/buyers", json={"name": "Bo"}, headers=api.h("dispositions")).status_code == 422
    assert api.post("/api/buyers", json={"name": "Bo", "email": "b@c.com"}, headers=api.h("acquisitions")).status_code == 403
    bid = b.json()["id"]
    assert api.put(f"/api/buyers/{bid}", json={"name": "Bo B", "email": "bo@cash.com", "approved": True, "proof_of_funds_verified": True,
                                              "criteria": {"states": ["TX"]}}, headers=api.h("dispositions")).status_code == 200
    off = api.patch(f"/api/buyers/{bid}/status", json={"proof_of_funds_verified": False}, headers=api.h("dispositions"))
    assert off.status_code == 200 and off.json()["proof_of_funds_verified"] is False
    assert api.get(f"/api/leads/{lid}/buyer-matches?analysis_id={aid}", headers=api.h("dispositions")).json() == []  # unverified buyers never match
    assert api.patch(f"/api/buyers/{bid}/status", json={"proof_of_funds_verified": True}, headers=api.h("viewer")).status_code == 403
    assert api.patch("/api/buyers/999/status", json={"active": True}, headers=api.h("dispositions")).status_code == 404
    api.patch(f"/api/buyers/{bid}/status", json={"proof_of_funds_verified": True}, headers=api.h("dispositions"))
    m = api.get(f"/api/leads/{lid}/buyer-matches?analysis_id={aid}", headers=api.h("dispositions")).json()
    assert [x["buyer"]["id"] for x in m] == [bid]
    blast = api.post(f"/api/leads/{lid}/buyer-blast", json={"analysis_id": aid, "buyer_ids": [bid], "channel": "email"}, headers=api.h("dispositions")).json()
    body = blast["payload"]["items"][0]["body"]
    assert "Jane" not in body and "512" not in body
    assert api.post(f"/api/approvals/{blast['id']}/decision", json={"approve": True}, headers=api.h("manager")).json()["status"] == "executed"
    assert api.post(f"/api/leads/{lid}/buyer-response", json={"buyer_id": bid, "status": "interested", "note": "called back"}, headers=api.h("dispositions")).status_code == 200
    assert api.post(f"/api/leads/{lid}/buyer-response", json={"buyer_id": bid, "status": "bogus"}, headers=api.h("dispositions")).status_code == 422


def test_agent_endpoints_use_llm_and_degrade_when_unavailable(api):
    lid = api.post("/api/leads", json=LEAD, headers=api.h("acquisitions")).json()["lead"]["id"]
    set_llm(ScriptedLLM([tool_use("draft_reply", {"channel": "sms", "body": "Hi Jane, thanks for reaching out. Is the house occupied?"}),
                         tool_use("save_summary", {"summary": "Seller wants to sell quickly; occupancy unknown."}, "t2")], [text("done")]))
    r = api.post(f"/api/leads/{lid}/agent/outreach", headers=api.h("acquisitions"))
    assert r.status_code == 200 and r.json()["draft_message_id"]
    assert len(api.get("/api/approvals", headers=api.h("viewer")).json()) == 1

    class Down:
        def create(self, **kw):
            from bravo.agents.llm import LLMUnavailable

            raise LLMUnavailable("no key")

    set_llm(Down())
    assert api.post(f"/api/leads/{lid}/agent/reply", headers=api.h("acquisitions")).status_code == 503
    assert api.post(f"/api/leads/{lid}/agent/reply", headers=api.h("viewer")).status_code == 403

    set_llm(ScriptedLLM([tool_use("calculate_mao", {"address": "x", "arv": 200000, "repair_estimate": 30000, "closing_costs": 4000, "assignment_fee": 10000})], [text("ok")]))
    d = api.post("/api/deals/analyze-text", json={"text": "x ARV 200000 repairs 30000 closing 4000 fee 10000"}, headers=api.h("acquisitions")).json()
    assert d["analysis_id"] and "$96,000.00" in d["summary"]


def test_agent_rate_limit(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "api_rate_limit_per_minute", 12)
    deps.reset_limiters()  # agent limit = 12 // 6 = 2 per minute
    lid = api.post("/api/leads", json=LEAD, headers=api.h("acquisitions")).json()["lead"]["id"]
    set_llm(ScriptedLLM(*[[text("ok")]] * 6))
    codes = [api.post(f"/api/leads/{lid}/agent/reply", headers=api.h("acquisitions")).status_code for _ in range(4)]
    assert 429 in codes and codes[0] == 200


def test_global_rate_limit(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "api_rate_limit_per_minute", 3)
    deps.reset_limiters()
    codes = [api.get("/api/leads", headers=api.h("viewer")).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_twilio_webhook_signature_stop_and_reply(api, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "twilio_auth_token", "tok")
    monkeypatch.setattr(s, "public_base_url", "https://jarvis.example.com")
    lid = api.post("/api/leads", json=LEAD, headers=api.h("acquisitions")).json()["lead"]["id"]
    url = "https://jarvis.example.com/api/webhooks/twilio/sms"

    def post(body, sid, signed=True):
        params = {"From": "+15128675309", "Body": body, "MessageSid": sid}
        sig = RequestValidator("tok").compute_signature(url, params) if signed else "bad"
        return api.post("/api/webhooks/twilio/sms", data=params, headers={"X-Twilio-Signature": sig})

    assert post("hello", "SM1", signed=False).status_code == 403
    r = post("Need to sell fast", "SM2")
    assert r.status_code == 200 and "<Response" in r.text
    assert len(api.get(f"/api/leads/{lid}/messages", headers=api.h("viewer")).json()) == 1
    assert post("STOP", "SM3").text.count("<Message>") == 0
    assert api.get(f"/api/leads/{lid}", headers=api.h("viewer")).json()["status"] == "do_not_contact"
    assert "<Message>" in post("HELP", "SM4").text


def test_web_form_key_and_honeypot(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "webform_key", "secret-key")
    form = {"Full Name": "Pat", "Phone": "512-867-5309", "Property Address": "5 Pine Rd", "Zip": "78701"}
    assert api.post("/api/intake/web-form", json=form).status_code == 401
    assert api.post("/api/intake/web-form", json=form, headers={"X-Form-Key": "wrong"}).status_code == 401
    assert api.post("/api/intake/web-form", json={**form, "website": "spam.com"}, headers={"X-Form-Key": "secret-key"}).json() == {"ok": True}
    assert api.get("/api/leads", headers=api.h("admin")).json() == []
    assert api.post("/api/intake/web-form", json=form, headers={"X-Form-Key": "secret-key"}).json()["created"] is True


def test_dashboard_tasks_activity_followup(api):
    hot = {**LEAD, "condition": "needs roof repairs", "price_expectation": "open to offers", "occupancy": "vacant"}
    api.post("/api/leads", json=hot, headers=api.h("acquisitions"))
    d = api.get("/api/dashboard", headers=api.h("viewer")).json()
    assert d["hot_leads"] == 1 and d["total_leads"] == 1 and d["open_tasks"] >= 1
    assert api.post("/api/followup/run", headers=api.h("acquisitions")).status_code == 200
    tasks = api.get("/api/tasks", headers=api.h("viewer")).json()
    assert api.post(f"/api/tasks/{tasks[0]['id']}/complete", headers=api.h("viewer")).status_code == 403
    assert api.post(f"/api/tasks/{tasks[0]['id']}/complete", headers=api.h("acquisitions")).json()["status"] == "done"
    ev = api.get("/api/activity?event=lead.ingested", headers=api.h("viewer")).json()
    assert ev and ev[0]["data"]["score"] >= 70
    assert api.get("/api/activity", headers=api.h("dispositions")).status_code == 403
    assert api.get("/api/appointments", headers=api.h("viewer")).status_code == 200


def test_campaign_endpoint(api):
    lid = api.post("/api/leads", json=LEAD, headers=api.h("acquisitions")).json()["lead"]["id"]
    ok = api.post("/api/campaigns", json={"name": "N", "lead_ids": [lid], "template": "Hi {first_name}, any update on {street}?", "channel": "sms"}, headers=api.h("manager"))
    assert ok.status_code == 200 and ok.json()["kind"] == "campaign"
    assert api.post("/api/campaigns", json={"name": "N", "lead_ids": [lid], "template": "Guaranteed deal {first_name}", "channel": "sms"}, headers=api.h("manager")).status_code == 422
    assert api.post("/api/campaigns", json={"name": "N", "lead_ids": [lid], "template": "Hi {first_name} hello", "channel": "sms"}, headers=api.h("dispositions")).status_code == 403


def test_patch_lead_dnc_opts_out(api):
    lid = api.post("/api/leads", json=LEAD, headers=api.h("acquisitions")).json()["lead"]["id"]
    r = api.patch(f"/api/leads/{lid}", json={"status": "do_not_contact"}, headers=api.h("acquisitions"))
    assert r.json()["status"] == "do_not_contact"
    d = api.post(f"/api/leads/{lid}/messages/draft", json={"channel": "sms", "body": "Hello again"}, headers=api.h("acquisitions"))
    assert d.status_code == 422
