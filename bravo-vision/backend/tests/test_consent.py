from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

from bravo import approvals, db
from bravo.api import deps
from bravo.config import Settings, get_settings
from bravo.crypto import Crypto, derive_key
from bravo.enums import ApprovalStatus, MessageStatus, Role
from bravo.integrations import registry
from bravo.integrations.base import SendResult
from bravo.main import create_app
from bravo.models import Message, User
from bravo.services import buyers, followup, inbound, leads, outbound

TUE_NOON = datetime(2026, 3, 3, 18, 0, tzinfo=UTC)


@pytest.fixture
def sms(monkeypatch):
    sent = []
    registry.configure(sms=type("S", (), {"send": lambda self, to, body: sent.append(body) or SendResult("x")})())
    monkeypatch.setattr(outbound, "utcnow", lambda: TUE_NOON)
    return sent


def mk(session, **kw):
    return leads.ingest_lead(session, {"phone": "512-867-5309", "street": "1 A St", "zip": "78701", "name": "Jane", **kw}, source="csv").lead


def test_consent_required_blocks_draft_and_campaign_and_send(session, sms, monkeypatch):
    monkeypatch.setattr(get_settings(), "require_consent", True)
    lead = mk(session)
    with pytest.raises(outbound.ComplianceError, match="consent"):
        outbound.draft_message(session, lead, "sms", "Hi Jane, still selling?", created_by="agent:x")
    with pytest.raises(followup.CampaignError, match="no_consent"):
        followup.propose_campaign(session, name="n", lead_ids=[lead.id], template="Hi {first_name}", channel="sms", requested_by="a", now=TUE_NOON)
    leads.record_consent(session, lead, sms=True, voice=None, source="web form 12345", actor="user:1")
    msg = outbound.draft_message(session, lead, "sms", "Hi Jane, still selling?", created_by="agent:x")
    lead.sms_consent = False  # consent revoked after the draft was approved-pending
    approvals.decide(session, msg.approval_id, approve=True, user_label="u", user_role=Role.ADMIN)
    assert msg.status == MessageStatus.BLOCKED and msg.error == "no recorded SMS consent" and sms == []


def test_consent_from_ingest_and_inbound_reply(session, sms, monkeypatch):
    monkeypatch.setattr(get_settings(), "require_consent", True)
    ok = mk(session, sms_consent="true", consent_source="Seller web form submission ID 12345")
    assert ok.sms_consent and ok.consent_source.endswith("12345") and not ok.voice_consent
    other = leads.ingest_lead(session, {"phone": "415-867-5309", "street": "2 B St", "zip": "78701"}, source="csv").lead
    assert not other.sms_consent
    inbound.handle_inbound_sms(session, "+14158675309", "yes I want to sell", "SM1")
    assert other.sms_consent and other.consent_source == "seller initiated SMS"
    dup = leads.ingest_lead(session, {"phone": "415-867-5309", "street": "2 B St", "zip": "78701", "voice_consent": "yes"}, source="web_form")
    assert dup.duplicate_of.voice_consent


def test_buyer_blast_needs_buyer_sms_consent(session, sms, monkeypatch):
    from tests.test_ops import mk_analysis, mk_buyer

    monkeypatch.setattr(get_settings(), "require_consent", True)
    lead = mk(session)
    analysis = mk_analysis(session, lead)
    b = mk_buyer(session)
    with pytest.raises(buyers.BuyerError, match="no_consent"):
        buyers.propose_blast(session, lead_id=lead.id, analysis_id=analysis.id, buyer_ids=[b.id], channel="sms", requested_by="a")


def test_kill_switch_holds_approved_messages(session, sms, monkeypatch):
    lead = mk(session)
    monkeypatch.setattr(get_settings(), "automatic_messaging_enabled", False)
    msg = outbound.draft_message(session, lead, "sms", "Hi Jane, still selling?", created_by="agent:x")
    appr = approvals.decide(session, msg.approval_id, approve=True, user_label="u", user_role=Role.ADMIN)
    assert appr.status == ApprovalStatus.EXECUTED and msg.status == MessageStatus.SCHEDULED and sms == []
    monkeypatch.setattr(get_settings(), "automatic_messaging_enabled", True)
    res = outbound.dispatch_due(session, now=TUE_NOON + timedelta(minutes=10))
    assert res[0]["status"] == "sent" and len(sms) == 1


def test_consent_endpoint(monkeypatch):
    db.init_engine("sqlite://")
    db.create_all()
    t = deps.new_token()
    with db.session_scope() as s:
        s.add(User(email="a@x.com", role="acquisitions", token_hash=deps.hash_token(t)))
        v = deps.new_token()
        s.add(User(email="v@x.com", role="viewer", token_hash=deps.hash_token(v)))
    h = {"Authorization": f"Bearer {t}"}
    with TestClient(create_app()) as c:
        lid = c.post("/api/leads", json={"phone": "512-867-5309", "street": "1 A St", "zip": "78701"}, headers=h).json()["lead"]["id"]
        body = {"sms_consent": True, "voice_consent": False, "source": "Seller web form submission ID 12345"}
        assert c.post(f"/api/leads/{lid}/consent", json=body, headers={"Authorization": f"Bearer {v}"}).status_code == 403
        r = c.post(f"/api/leads/{lid}/consent", json=body, headers=h)
        assert r.json()["sms_consent"] is True and r.json()["voice_consent"] is False
        assert c.post(f"/api/leads/{lid}/consent", json={"source": "abc"}, headers=h).status_code == 422
        assert c.get(f"/api/leads/{lid}", headers=h).json()["consent_source"].endswith("12345")
        ev = c.get("/api/activity?event=lead.consent_recorded", headers=h).json()
        assert ev[0]["data"]["sms"] is True
    db.Base.metadata.drop_all(db.get_engine())


def test_env_names_and_quiet_hours(monkeypatch):
    monkeypatch.delenv("BV_DATABASE_URL")  # BV_-prefixed names win over the plain aliases
    for k, v in {"DATABASE_URL": "postgresql+psycopg://postgres:pw@db:5432/wholesale_ops", "APP_TIMEZONE": "America/Chicago",
                 "QUIET_HOURS_START": "20:00", "QUIET_HOURS_END": "09:00", "RATE_LIMIT_PER_MINUTE": "60", "ANTHROPIC_MODEL": "claude-3-7-sonnet-latest",
                 "TWILIO_FROM_NUMBER": "+15125550100", "AUTOMATIC_MESSAGING_ENABLED": "false", "AUTOMATIC_VOICE_ENABLED": "false",
                 "VOICE_MAX_DURATION_SECONDS": "600", "APP_SECRET_KEY": "x" * 40}.items():
        monkeypatch.setenv(k, v)
    s = Settings(_env_file=None)
    assert s.database_url.startswith("postgresql+psycopg://") and s.api_rate_limit_per_minute == 60
    assert (s.contact_start_hour, s.contact_end_hour) == (9, 20) and s.claude_model == "claude-3-7-sonnet-latest"
    assert s.automatic_messaging_enabled is False and s.automatic_voice_enabled is False and s.twilio_from_number == "+15125550100"
    assert Settings(_env_file=None, env="production").is_production  # APP_SECRET_KEY satisfies the production key requirement
    monkeypatch.setenv("QUIET_HOURS_END", "09:30")
    with pytest.raises(ValueError, match="whole hours"):
        Settings(_env_file=None)


def test_app_secret_derives_stable_encryption_key():
    k = derive_key("a-long-random-secret-value")
    assert k == derive_key("a-long-random-secret-value") != derive_key("other")
    assert Crypto([k]).decrypt(Crypto([k]).encrypt("pii")) == "pii" and len(Fernet(k.encode()).encrypt(b"x")) > 0
    assert Settings(_env_file=None).consent_required is False and Settings(_env_file=None, env="production", app_secret_key="s" * 32,
                                                                           database_url="postgresql://x").consent_required is True


def test_jarvis_knows_the_owner_as_bryce(session):
    from bravo.agents.base import Agent

    assert get_settings().owner_name == "Bryce"
    prompt = Agent().system_prompt()
    assert "Bryce" in prompt and "Never use the owner's name" in prompt


def test_texas_central_time_defaults(monkeypatch):
    from bravo.services.hours import contact_tz, is_contact_allowed

    assert str(contact_tz("TX")) == "America/Chicago" and get_settings().business_tz == "America/Chicago"
    assert str(contact_tz(None)) == "America/Chicago"  # unknown state falls back to the owner's Texas time
    assert is_contact_allowed(datetime(2026, 7, 1, 14, 30, tzinfo=UTC), "TX")        # 9:30am CDT
    assert not is_contact_allowed(datetime(2026, 7, 1, 1, 30, tzinfo=UTC), "TX")     # 8:30pm CDT the night before
    assert not is_contact_allowed(datetime(2026, 7, 5, 18, 0, tzinfo=UTC), "TX")     # Sunday
