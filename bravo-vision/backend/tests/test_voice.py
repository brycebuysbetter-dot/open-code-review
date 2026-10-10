import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from bravo import approvals, db
from bravo.agents.testing import ScriptedLLM, text, tool_use
from bravo.agents.voice import VoiceAgent
from bravo.api import deps
from bravo.config import get_settings
from bravo.enums import ApprovalStatus, LeadStatus, Role
from bravo.integrations import registry
from bravo.integrations.base import PermanentSendError
from bravo.integrations.twilio_sms import TwilioVoice
from bravo.main import create_app
from bravo.models import Lead, Task, User
from bravo.services import leads, voice

TUE_NOON = datetime(2026, 3, 3, 18, 0, tzinfo=UTC)
TUE_NIGHT = datetime(2026, 3, 4, 3, 0, tzinfo=UTC)


class FakeVoice:
    def __init__(self):
        self.calls = []

    def place_call(self, to, url, status_callback, time_limit):
        self.calls.append((to, url, status_callback, time_limit))
        return "CA123"


@pytest.fixture
def fv(monkeypatch):
    f = FakeVoice()
    registry.configure(voice=f)
    monkeypatch.setattr(get_settings(), "public_base_url", "https://jarvis.example.com")
    monkeypatch.setattr(voice, "utcnow", lambda: TUE_NOON)
    return f


def mk(session, consent=True, **kw):
    rec = {"phone": "512-867-5309", "street": "1 A St", "zip": "78701", "state": "TX", "name": "Jane Doe", **kw}
    if consent:
        rec["voice_consent"] = "true"
    return leads.ingest_lead(session, rec, source="web_form").lead


def approve(session, call, now=TUE_NOON, monkeypatch=None):
    return approvals.decide(session, call.approval_id, approve=True, user_label="user:2", user_role=Role.MANAGER)


def test_request_requires_voice_consent_and_blocks_duplicates(session, fv):
    with pytest.raises(voice.VoiceError, match="voice consent"):
        voice.request_call(session, mk(session, consent=False), goal="intro", requested_by="user:1")
    lead = mk(session, phone="415-867-5309", street="2 B St")
    call = voice.request_call(session, lead, goal="intro", requested_by="user:1")
    assert "automated assistant" in call.opening and "say stop" in call.opening and "Jane" in call.opening
    with pytest.raises(voice.VoiceError, match="already pending"):
        voice.request_call(session, lead, goal="again", requested_by="user:1")
    assert fv.calls == []  # nothing placed before approval


def test_approved_call_is_placed_with_webhook_urls_and_time_limit(session, fv):
    call = voice.request_call(session, mk(session), goal="intro", requested_by="user:1")
    a = approve(session, call)
    assert a.status == ApprovalStatus.EXECUTED and call.status == "queued" and call.twilio_sid == "CA123"
    to, url, cb, limit = fv.calls[0]
    assert to == "+15128675309" and url.endswith(f"/api/webhooks/twilio/voice-turn?call_id={call.id}") and limit == 600


def test_call_deferred_outside_hours_held_by_switch_and_daily_cap(session, fv, monkeypatch):
    lead = mk(session)
    c1 = voice.request_call(session, lead, goal="intro", requested_by="user:1")
    monkeypatch.setattr(voice, "utcnow", lambda: TUE_NIGHT)
    approve(session, c1)
    assert c1.status == "scheduled" and fv.calls == []
    assert voice.dispatch_calls(session, now=TUE_NIGHT) == []
    res = voice.dispatch_calls(session, now=c1.scheduled_for + timedelta(minutes=1))
    assert res[0]["status"] == "queued" and len(fv.calls) == 1
    # kill switch
    lead2 = mk(session, phone="415-867-5309", street="2 B St")
    monkeypatch.setattr(get_settings(), "automatic_voice_enabled", False)
    c2 = voice.request_call(session, lead2, goal="intro", requested_by="user:1")
    approve(session, c2)
    assert c2.status == "scheduled" and len(fv.calls) == 1
    # one call per lead per day
    monkeypatch.setattr(get_settings(), "automatic_voice_enabled", True)
    c1.status = "completed"
    c3 = voice.request_call(session, lead, goal="again", requested_by="user:1")
    approve(session, c3)
    assert c3.status == "scheduled" and "one call per lead" not in (c3.error or "") and len(fv.calls) == 1


def test_opt_out_or_missing_config_blocks_placement(session, fv, monkeypatch):
    lead = mk(session)
    call = voice.request_call(session, lead, goal="intro", requested_by="user:1")
    lead.voice_consent = False  # revoked before approval
    assert approve(session, call).status == ApprovalStatus.FAILED and call.error == "no recorded voice consent"
    lead2 = mk(session, phone="415-867-5309", street="2 B St")
    c2 = voice.request_call(session, lead2, goal="x", requested_by="user:1")
    monkeypatch.setattr(get_settings(), "public_base_url", "")
    assert approve(session, c2).status == ApprovalStatus.FAILED and "PUBLIC_BASE_URL" in c2.error
    assert fv.calls == []


def test_provider_failure_marks_failed(session, fv, monkeypatch):
    class Boom:
        def place_call(self, *a):
            raise PermanentSendError("invalid number")

    registry.configure(voice=Boom())
    call = voice.request_call(session, mk(session), goal="intro", requested_by="user:1")
    assert approve(session, call).status == ApprovalStatus.FAILED and call.status == "failed"


def test_reject_cancels_call(session, fv):
    call = voice.request_call(session, mk(session), goal="intro", requested_by="user:1")
    approvals.decide(session, call.approval_id, approve=False, user_label="u", user_role=Role.ADMIN)
    assert call.status == "rejected" and fv.calls == []


def _live(session, fv):
    call = voice.request_call(session, mk(session), goal="intro", requested_by="user:1")
    approve(session, call)
    return call


def test_conversation_turns_discovery_and_compliance(session, fv):
    call = _live(session, fv)
    first = voice.handle_turn(session, call.id, None)
    assert first["say"] == call.opening and call.status == "in_progress" and not first["hangup"]
    llm = ScriptedLLM([tool_use("record_discovery", {"timeline": "within a month", "reason": "divorce"})],
                      [text("Thanks for sharing that. Is the house currently occupied?")])
    out = voice.handle_turn(session, call.id, "Yes, I need to sell within a month because of a divorce", agent=VoiceAgent(llm))
    lead = session.get(Lead, call.lead_id)
    assert out == {"say": "Thanks for sharing that. Is the house currently occupied?", "hangup": False}
    assert lead.discovery["timeline"] == "within a month" and lead.motivation_score >= 40
    transcript = json.loads(call.transcript)
    assert [t["role"] for t in transcript] == ["assistant", "seller", "assistant"]
    assert {t["name"] for t in llm.calls[0]["tools"]} == {"get_lead_context", "record_discovery", "update_lead_status", "end_call"}  # no send/offer tools


def test_noncompliant_spoken_reply_is_replaced_and_escalated(session, fv):
    call = _live(session, fv)
    voice.handle_turn(session, call.id, None)
    out = voice.handle_turn(session, call.id, "How much will you pay?", agent=VoiceAgent(ScriptedLLM([text("We guarantee $150,000 cash")])))
    assert out["hangup"] and "team will follow up" in out["say"] and "150" not in out["say"]
    assert session.query(Task).filter(Task.kind == "call_review", Task.priority == 1).count() == 1


def test_stop_request_opts_out_everything_and_hangs_up(session, fv):
    call = _live(session, fv)
    voice.handle_turn(session, call.id, None)
    out = voice.handle_turn(session, call.id, "Please stop calling me")
    assert out["hangup"] and "won't be called again" in out["say"]
    from bravo.services.optout import is_opted_out

    assert is_opted_out(session, "voice", "+15128675309") and is_opted_out(session, "sms", "+15128675309")
    assert session.get(Lead, call.lead_id).status == LeadStatus.DO_NOT_CONTACT


def test_turn_limit_agent_failure_and_dead_calls(session, fv, monkeypatch):
    call = _live(session, fv)
    voice.handle_turn(session, call.id, None)

    class Down:
        def create(self, **kw):
            raise RuntimeError("llm down")

    out = voice.handle_turn(session, call.id, "hello?", agent=VoiceAgent(Down()))
    assert out["hangup"] and session.query(Task).filter(Task.kind == "call_review").count() == 1
    call2 = _live_second(session, fv)
    voice.handle_turn(session, call2.id, None)
    monkeypatch.setattr(get_settings(), "voice_max_turns", 1)
    assert voice.handle_turn(session, call2.id, "hi there")["hangup"]
    assert voice.handle_turn(session, 9999, "x") == {"say": "", "hangup": True}
    assert call.status == "in_progress"  # a failed agent turn hands off to a human; Twilio ends the call on hangup


def _live_second(session, fv):
    call = voice.request_call(session, mk(session, phone="415-867-5309", street="2 B St"), goal="intro", requested_by="user:1")
    approve(session, call)
    return call


def test_silence_and_status_callback(session, fv):
    call = _live(session, fv)
    voice.handle_turn(session, call.id, None)
    assert voice.handle_turn(session, call.id, "  ")["hangup"]
    call.turns = 2
    voice.handle_status(session, call.id, "completed", 74)
    assert call.status == "completed" and call.duration_seconds == 74
    assert session.query(Task).filter(Task.title.like("Review AI call%")).count() == 1
    voice.handle_status(session, call.id, "ringing", None)
    voice.handle_status(session, 424242, "completed", 1)
    assert call.status == "completed"
    c2 = _live_second(session, fv)
    voice.handle_status(session, c2.id, "no-answer", None)
    assert c2.status == "no_answer"


def test_twilio_voice_gateway_errors():
    class C:
        def __init__(self, err):
            self.err = err
            self.calls = type("X", (), {"create": self.create})()

        def create(self, **kw):
            if self.err:
                raise self.err.pop(0)
            return type("Call", (), {"sid": "CA9"})()

    ok = TwilioVoice(C([TwilioRestException(503, "u")]), "+1555", sleep=lambda s: None)
    assert ok.place_call("+1512", "u", "cb", 300) == "CA9"
    with pytest.raises(PermanentSendError):
        TwilioVoice(C([TwilioRestException(400, "u", "bad")]), "+1555", sleep=lambda s: None).place_call("x", "u", "cb", 1)


@pytest.fixture
def api(monkeypatch):
    db.init_engine("sqlite://")
    db.create_all()
    tok = {}
    with db.session_scope() as s:
        for role in ("acquisitions", "viewer", "manager"):
            t = deps.new_token()
            s.add(User(email=f"{role}@x.com", role=role, token_hash=deps.hash_token(t)))
            tok[role] = t
    monkeypatch.setattr(voice, "utcnow", lambda: TUE_NOON)
    monkeypatch.setattr(get_settings(), "public_base_url", "https://jarvis.example.com")
    monkeypatch.setattr(get_settings(), "twilio_auth_token", "tok")
    registry.configure(voice=FakeVoice())
    with TestClient(create_app()) as c:
        c.h = lambda r: {"Authorization": f"Bearer {tok[r]}"}
        yield c
    db.Base.metadata.drop_all(db.get_engine())


def test_voice_over_http_with_signed_webhooks(api):
    lid = api.post("/api/leads", json={"name": "Jane", "phone": "512-867-5309", "street": "1 A St", "zip": "78701", "state": "TX"}, headers=api.h("acquisitions")).json()["lead"]["id"]
    assert api.post(f"/api/leads/{lid}/calls/request", json={"goal": "intro call"}, headers=api.h("acquisitions")).status_code == 422
    api.post(f"/api/leads/{lid}/consent", json={"voice_consent": True, "source": "Seller web form submission ID 12345"}, headers=api.h("acquisitions"))
    assert api.post(f"/api/leads/{lid}/calls/request", json={"goal": "intro call"}, headers=api.h("viewer")).status_code == 403
    r = api.post(f"/api/leads/{lid}/calls/request", json={"goal": "intro call"}, headers=api.h("acquisitions")).json()
    pend = api.get("/api/approvals", headers=api.h("viewer")).json()[0]
    assert pend["kind"] == "call" and pend["payload"]["opening"] == r["opening"]
    assert api.post(f"/api/approvals/{r['approval_id']}/decision", json={"approve": True}, headers=api.h("manager")).json()["status"] == "executed"

    cid = r["call_id"]
    url = f"https://jarvis.example.com/api/webhooks/twilio/voice-turn?call_id={cid}"

    def post(path, params, signed=True):
        full = f"https://jarvis.example.com/api/webhooks/twilio/{path}?call_id={cid}"
        sig = RequestValidator("tok").compute_signature(full, params) if signed else "bad"
        return api.post(f"/api/webhooks/twilio/{path}?call_id={cid}", data=params, headers={"X-Twilio-Signature": sig})

    assert post("voice-turn", {"CallSid": "CA123"}, signed=False).status_code == 403
    first = post("voice-turn", {"CallSid": "CA123"})
    assert first.status_code == 200 and "<Gather" in first.text and "automated assistant" in first.text and url.split("?")[0] in first.text
    bye = post("voice-turn", {"CallSid": "CA123", "SpeechResult": "stop calling me"})
    assert "<Hangup/>" in bye.text and "<Gather" not in bye.text
    assert post("voice-status", {"CallStatus": "completed", "CallDuration": "42"}).status_code == 204
    assert post("voice-status", {"CallStatus": "completed"}, signed=False).status_code == 403
    calls = api.get(f"/api/leads/{lid}/calls", headers=api.h("viewer")).json()
    assert calls[0]["status"] == "completed" and calls[0]["transcript"][1]["text"] == "stop calling me"
    assert api.get(f"/api/leads/{lid}", headers=api.h("viewer")).json()["status"] == "do_not_contact"
