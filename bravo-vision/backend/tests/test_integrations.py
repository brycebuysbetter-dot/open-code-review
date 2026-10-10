import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import httplib2
import pytest
from googleapiclient.errors import HttpError
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from bravo import cli, db
from bravo.agents.llm import AnthropicLLM, LLMUnavailable, get_llm, set_llm
from bravo.config import Settings, get_settings
from bravo.integrations import registry
from bravo.integrations.base import IntegrationNotConfigured, PermanentSendError
from bravo.integrations.crm.base import CRMContact, CRMDeal
from bravo.integrations.crm.hubspot import HubSpotCRM
from bravo.integrations.crm.local import LocalCRM
from bravo.integrations.google_api import GmailGateway, GoogleCalendar
from bravo.integrations.twilio_sms import TwilioSMS, valid_twilio_signature
from bravo.models import Lead, User
from bravo.retry import TransientError
from bravo.services import crm_sync, leads

NOSLEEP = lambda s: None  # noqa: E731


# ---- Twilio ----

class FakeTwilio:
    def __init__(self, errors):
        self.errors, self.calls = list(errors), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        if self.errors:
            raise self.errors.pop(0)
        return SimpleNamespace(sid="SM123")


def test_twilio_retries_transient_then_succeeds():
    c = FakeTwilio([TwilioRestException(503, "u"), TwilioRestException(429, "u")])
    res = TwilioSMS(c, "+15550001111", sleep=NOSLEEP).send("+15128675309", "hi")
    assert res.external_id == "SM123" and len(c.calls) == 3 and c.calls[0]["from_"] == "+15550001111"


def test_twilio_permanent_error_not_retried_and_exhaustion():
    c = FakeTwilio([TwilioRestException(400, "u", "bad number")])
    with pytest.raises(PermanentSendError):
        TwilioSMS(c, "+1", sleep=NOSLEEP).send("x", "y")
    assert len(c.calls) == 1
    c2 = FakeTwilio([TwilioRestException(500, "u")] * 5)
    with pytest.raises(TransientError):
        TwilioSMS(c2, "+1", sleep=NOSLEEP).send("x", "y")
    assert len(c2.calls) == 4


def test_twilio_not_configured_and_signature(monkeypatch):
    with pytest.raises(IntegrationNotConfigured):
        TwilioSMS()
    monkeypatch.setattr(get_settings(), "twilio_auth_token", "tok")
    sig = RequestValidator("tok").compute_signature("https://x/y", {"a": "1"})
    assert valid_twilio_signature("https://x/y", {"a": "1"}, sig)
    assert not valid_twilio_signature("https://x/y", {"a": "2"}, sig) and not valid_twilio_signature("https://x/y", {}, "")


# ---- Google ----

class Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeGmail:
    def __init__(self, fail=0, status=503):
        self.sent, self.fail, self.status = [], fail, status

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId, body):
        def go():
            if self.fail:
                self.fail -= 1
                raise HttpError(httplib2.Response({"status": self.status}), b"")
            self.sent.append(body)
            return {"id": "g1"}
        return Req(go)

    def list(self, userId, q, maxResults):
        return Req(lambda: {"messages": [{"id": "m1"}]})

    def get(self, userId, id, format):
        data = base64.urlsafe_b64encode(b"I want to sell 1 Main St").decode()
        return Req(lambda: {"payload": {"headers": [{"name": "From", "value": "A <a@b.com>"}, {"name": "Subject", "value": "Sell"}],
                                        "parts": [{"mimeType": "text/html", "body": {}}, {"mimeType": "text/plain", "body": {"data": data}}]}})


def test_gmail_send_retries_and_builds_mime():
    svc = FakeGmail(fail=2)
    res = GmailGateway(svc, "me@biz.com", sleep=NOSLEEP).send("a@b.com", "Hello", "Body text")
    assert res.external_id == "g1"
    raw = base64.urlsafe_b64decode(svc.sent[0]["raw"]).decode()
    assert "To: a@b.com" in raw and "Subject: Hello" in raw and "Body text" in raw


def test_gmail_permanent_error():
    with pytest.raises(PermanentSendError):
        GmailGateway(FakeGmail(fail=1, status=403), "me", sleep=NOSLEEP).send("a@b.com", "s", "b")


def test_gmail_fetch_inbound_parses_plain_text():
    em = GmailGateway(FakeGmail(), "me").fetch_inbound("label:leads")[0]
    assert em.sender == "A <a@b.com>" and em.subject == "Sell" and "1 Main St" in em.body


class FakeCal:
    def __init__(self):
        self.inserted = None

    def events(self):
        return self

    def insert(self, calendarId, body):
        self.inserted = body
        return Req(lambda: {"id": "evt9"})

    def freebusy(self):
        return self

    def query(self, body):
        return Req(lambda: {"calendars": {"primary": {"busy": [{"start": "2030-01-01T15:00:00Z", "end": "2030-01-01T16:00:00Z"}]}}})


def test_calendar_event_has_no_attendees_and_busy_parsing():
    cal = FakeCal()
    g = GoogleCalendar(cal, "primary", sleep=NOSLEEP)
    start, end = datetime(2030, 1, 2, 15, tzinfo=UTC), datetime(2030, 1, 2, 16, tzinfo=UTC)
    assert g.create_event("Call", start, end, "Phone", "desc") == "evt9"
    assert "attendees" not in cal.inserted
    busy = g.busy_periods(start, end)
    assert busy[0][0] == datetime(2030, 1, 1, 15, tzinfo=UTC)


def test_google_not_configured():
    with pytest.raises(IntegrationNotConfigured):
        GmailGateway()


# ---- HubSpot ----

def hubspot(handler):
    return HubSpotCRM("tok", transport=httpx.MockTransport(handler), sleep=NOSLEEP)


def test_hubspot_contact_upsert_search_then_patch_and_create():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        assert req.headers["authorization"] == "Bearer tok"
        if req.url.path.endswith("/search"):
            body = json.loads(req.content)
            return httpx.Response(200, json={"results": [{"id": "77"}] if body["filterGroups"][0]["filters"][0]["value"] == "a@b.com" else []})
        if req.method == "POST":
            return httpx.Response(201, json={"id": "88"})
        return httpx.Response(200, json={})

    crm = hubspot(handler)
    assert crm.upsert_contact(CRMContact(1, "Jane", "a@b.com", "+1512")) == "77"
    assert crm.upsert_contact(CRMContact(2, "Bob", "new@b.com")) == "88"
    assert crm.upsert_contact(CRMContact(3, "Cy", external_id="5")) == "5"
    assert ("PATCH", "/crm/v3/objects/contacts/77") in seen


def test_hubspot_deal_note_stage_and_fetch_leads():
    def handler(req):
        p = req.url.path
        if p == "/crm/v3/objects/deals" and req.method == "POST":
            assert json.loads(req.content)["associations"][0]["to"]["id"] == "77"
            return httpx.Response(201, json={"id": "d1"})
        if p == "/crm/v3/objects/contacts" and req.method == "GET":
            return httpx.Response(200, json={"results": [{"id": "9", "properties": {"firstname": "A", "lastname": "B", "email": "a@b.com", "address": "1 St"}}]})
        return httpx.Response(200, json={})

    crm = hubspot(handler)
    assert crm.upsert_deal(CRMDeal(1, "T", "new", contact_external_id="77")) == "d1"
    assert crm.upsert_deal(CRMDeal(1, "T", "new", external_id="d1")) == "d1"
    crm.add_note("77", "note")
    crm.update_stage("d1", "offer_sent")
    assert crm.fetch_leads()[0] == {"name": "A B", "email": "a@b.com", "phone": None, "street": "1 St", "city": None, "state": None, "zip": None, "crm_id": "9"}


def test_hubspot_retry_and_errors():
    calls = []

    def flaky(req):
        calls.append(1)
        return httpx.Response(429 if len(calls) < 3 else 200, json={"results": []})

    assert hubspot(flaky).fetch_leads() == [] and len(calls) == 3
    with pytest.raises(PermanentSendError):
        hubspot(lambda r: httpx.Response(401)).fetch_leads()
    with pytest.raises(IntegrationNotConfigured):
        HubSpotCRM()


def test_crm_outage_never_breaks_pipeline(session):
    class Down(LocalCRM):
        def upsert_contact(self, c):
            raise RuntimeError("crm down")

    registry.configure(crm=Down())
    lead = leads.ingest_lead(session, {"phone": "512-867-5309", "street": "1 A St", "zip": "78701"}, source="csv").lead
    assert crm_sync.sync_lead(session, lead) is False
    registry.configure(crm=LocalCRM())
    assert crm_sync.sync_lead(session, lead, note="n") is True and lead.crm_id == f"local-contact-{lead.id}"


def test_registry_provider_selection(monkeypatch):
    monkeypatch.setattr(get_settings(), "crm_provider", "local")
    assert isinstance(registry.crm(), LocalCRM)
    registry.reset()
    monkeypatch.setattr(get_settings(), "crm_provider", "nope")
    with pytest.raises(ValueError):
        registry.crm()
    monkeypatch.setattr(get_settings(), "crm_provider", "hubspot")
    with pytest.raises(IntegrationNotConfigured):
        registry.crm()
    for fn in (registry.sms, registry.email, registry.calendar):
        with pytest.raises(IntegrationNotConfigured):
            fn()


# ---- Anthropic wrapper ----

def test_anthropic_wrapper_normalizes_blocks_and_passes_tools():
    captured = {}

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                captured.update(kw)
                return SimpleNamespace(stop_reason="tool_use", content=[
                    SimpleNamespace(type="text", text="thinking"),
                    SimpleNamespace(type="tool_use", id="tu1", name="get_lead_context", input={"a": 1}),
                    SimpleNamespace(type="thinking")])

    out = AnthropicLLM(Client(), model="claude-sonnet-5-5").create(system="sys", messages=[{"role": "user", "content": "x"}],
                                                                  tools=[{"name": "t"}], max_tokens=100)
    assert out.stop_reason == "tool_use"
    assert out.content == [{"type": "text", "text": "thinking"}, {"type": "tool_use", "id": "tu1", "name": "get_lead_context", "input": {"a": 1}}]
    assert captured["model"] == "claude-sonnet-5-5" and captured["system"] == "sys" and captured["tools"] == [{"name": "t"}]


def test_llm_unavailable_without_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "")
    set_llm(None)
    with pytest.raises(LLMUnavailable):
        get_llm()


# ---- config / CLI ----

def test_production_requires_keys_and_postgres():
    with pytest.raises(ValueError, match="ENCRYPTION"):
        Settings(env="production", database_url="postgresql://x", encryption_keys="")
    with pytest.raises(ValueError, match="PostgreSQL"):
        Settings(env="production", encryption_keys="k", database_url="sqlite:///x.db")
    with pytest.raises(ValueError, match="hours"):
        Settings(contact_start_hour=20, contact_end_hour=9)
    assert Settings().contact_weekdays == {0, 1, 2, 3, 4, 5}


def test_cli_create_user_and_run_cycle(capsys):
    db.init_engine("sqlite://")
    db.create_all()
    assert cli.main(["create-user", "boss@x.com", "--role", "admin"]) == 0
    token = capsys.readouterr().out.strip().split(": ")[1]
    with db.session_scope() as s:
        assert s.query(User).one().token_hash and token.startswith("bv_")
        leads.ingest_lead(s, {"phone": "512-867-5309", "street": "1 A St", "zip": "78701"}, source="csv")
    out = cli.run_cycle()
    assert set(out) >= {"tasks_created", "dispatched"}
    assert cli.main(["init-db"]) == 0 and cli.main(["run-cycle"]) == 0
    db.Base.metadata.drop_all(db.get_engine())
    assert Lead  # silence unused import


def test_enc_secrets_roundtrip(capsys, monkeypatch):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    token = "enc:" + __import__("bravo.crypto", fromlist=["Crypto"]).Crypto([key]).encrypt("sk-real-key")
    cfg = Settings(encryption_keys=key, anthropic_api_key=token, twilio_auth_token="plain")
    assert cfg.anthropic_api_key == "sk-real-key" and cfg.twilio_auth_token == "plain"
    with pytest.raises(ValueError, match="DECRYPT|decrypt"):
        Settings(encryption_keys="", anthropic_api_key=token)
    with pytest.raises(ValueError):
        Settings(encryption_keys=Fernet.generate_key().decode(), anthropic_api_key=token)  # wrong key
    assert cli.main(["encrypt-secret", "abc"]) == 0
    assert capsys.readouterr().out.startswith("enc:")


# ---- Bravo Vision sync ----

def test_bravo_adapter_signs_requests_and_retries():
    from bravo.integrations.crm.bravo import BravoVisionCRM, sign

    seen = []

    def handler(req):
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(503)
        if req.method == "GET":
            return httpx.Response(200, json={"leads": [{"external_id": "b1", "phone": "512-867-5309"}]})
        return httpx.Response(200, json={"id": "BV-9"})

    crm = BravoVisionCRM("https://bravo.example.com", "apikey", "synckey", transport=httpx.MockTransport(handler), sleep=NOSLEEP)
    assert crm.upsert_contact(CRMContact(1, "Jane", "a@b.com", "+1512")) == "BV-9" and len(seen) == 2
    post = seen[1]
    assert post.headers["authorization"] == "Bearer apikey" and post.headers["x-jarvis-signature"] == sign(post.content, "synckey")
    assert json.loads(post.content)["event"] == "contact_upsert"
    assert crm.upsert_deal(CRMDeal(1, "T", "new")) == "BV-9"
    crm.add_note("BV-9", "n")
    crm.update_stage("BV-9", "offer_sent")
    assert crm.fetch_leads()[0]["external_id"] == "b1"
    with pytest.raises(PermanentSendError):
        BravoVisionCRM("https://x", "k", "s", transport=httpx.MockTransport(lambda r: httpx.Response(403)), sleep=NOSLEEP).fetch_leads()
    with pytest.raises(IntegrationNotConfigured):
        BravoVisionCRM()


def test_registry_selects_bravo(monkeypatch):
    s = get_settings()
    for k, v in (("crm_provider", "bravo"), ("bravo_base_url", "https://b.example.com"), ("bravo_api_key", "a"), ("bravo_sync_key", "s")):
        monkeypatch.setattr(s, k, v)
    registry.reset()
    assert registry.crm().name == "bravo"
