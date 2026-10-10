from datetime import UTC, datetime, timedelta

import pytest

from bravo import approvals
from bravo.config import get_settings
from bravo.enums import ApprovalStatus, LeadStatus, MessageStatus, Role
from bravo.integrations import registry
from bravo.integrations.base import PermanentSendError, SendResult
from bravo.models import Approval, Conversation, Message
from bravo.retry import TransientError
from bravo.services import inbound, leads, optout, outbound

TUE_NOON = datetime(2026, 3, 3, 18, 0, tzinfo=UTC)  # 12:00 Chicago
TUE_NIGHT = datetime(2026, 3, 4, 3, 0, tzinfo=UTC)  # Tue 9pm Chicago


class FakeSMS:
    def __init__(self):
        self.sent, self.error = [], None

    def send(self, to, body):
        if self.error:
            raise self.error
        self.sent.append((to, body))
        return SendResult(f"SM{len(self.sent)}")


@pytest.fixture
def sms():
    fake = FakeSMS()
    registry.configure(sms=fake)
    yield fake
    registry.reset()


@pytest.fixture
def lead(session):
    r = leads.ingest_lead(session, {"name": "Jane", "phone": "512-867-5309", "street": "12 Oak Lane", "city": "Austin",
                                    "state": "TX", "zip": "78701", "timeline": "2 weeks", "reason": "foreclosure"}, source="web_form")
    session.commit()
    return r.lead


def approve(session, msg, now=TUE_NOON, monkeypatch=None):
    return approvals.decide(session, msg.approval_id, approve=True, user_label="user:9", user_role=Role.MANAGER)


def test_nothing_sends_without_approval(session, lead, sms):
    msg = outbound.draft_message(session, lead, "sms", "Hi Jane, still thinking about selling?", created_by="agent:seller")
    assert msg.status == MessageStatus.PENDING_APPROVAL and sms.sent == []
    assert "Reply STOP" in msg.body
    assert session.get(Approval, msg.approval_id).status == ApprovalStatus.PENDING


def test_approved_message_sends_in_hours(session, lead, sms, monkeypatch):
    monkeypatch.setattr(outbound, "utcnow", lambda: TUE_NOON)
    msg = outbound.draft_message(session, lead, "sms", "Hi Jane, still thinking about selling?", created_by="agent:seller")
    a = approve(session, msg)
    assert a.status == ApprovalStatus.EXECUTED and sms.sent[0][0] == "+15128675309"
    assert msg.status == MessageStatus.SENT and lead.status == LeadStatus.CONTACTED


def test_outside_hours_is_scheduled_then_dispatched(session, lead, sms, monkeypatch):
    monkeypatch.setattr(outbound, "utcnow", lambda: TUE_NIGHT)
    msg = outbound.draft_message(session, lead, "sms", "Hi Jane, following up.", created_by="agent:followup")
    approve(session, msg)
    assert msg.status == MessageStatus.SCHEDULED and sms.sent == []
    assert outbound.dispatch_due(session, now=TUE_NIGHT) == []
    res = outbound.dispatch_due(session, now=msg.scheduled_for + timedelta(minutes=1))
    assert res[0]["status"] == "sent" and len(sms.sent) == 1


def test_draft_rejected_for_compliance(session, lead, sms):
    with pytest.raises(outbound.ComplianceError) as e:
        outbound.draft_message(session, lead, "sms", "We guarantee $90,000 cash", created_by="agent:seller")
    assert len(e.value.violations) == 2
    assert session.query(Message).count() == 0


def test_opt_out_blocks_and_cascades(session, lead, sms, monkeypatch):
    monkeypatch.setattr(outbound, "utcnow", lambda: TUE_NOON)
    msg = outbound.draft_message(session, lead, "sms", "Hi Jane, checking in.", created_by="agent:seller")
    res = inbound.handle_inbound_sms(session, "+1 (512) 867-5309", "STOP", "SM_in_1")
    assert res.action == "stop" and res.reply is None
    assert lead.status == LeadStatus.DO_NOT_CONTACT and msg.status == MessageStatus.BLOCKED
    assert session.get(Approval, msg.approval_id).status == ApprovalStatus.REJECTED
    with pytest.raises(outbound.ComplianceError):
        outbound.draft_message(session, lead, "sms", "Hello again", created_by="agent:seller")
    assert sms.sent == []
    assert inbound.handle_inbound_sms(session, "5128675309", "start").action == "start"
    assert not optout.is_opted_out(session, "sms", "+15128675309")


def test_send_time_guard_blocks_even_if_cascade_missed(session, lead, sms, monkeypatch):
    monkeypatch.setattr(outbound, "utcnow", lambda: TUE_NIGHT)
    msg = outbound.draft_message(session, lead, "sms", "Hi Jane.", created_by="agent:seller")
    approve(session, msg)
    assert msg.status == MessageStatus.SCHEDULED
    optout.record_opt_out(session, "sms", "+15128675309")
    assert msg.status == MessageStatus.BLOCKED  # cascade
    msg.status, lead.status = MessageStatus.SCHEDULED, LeadStatus.CONTACTED  # defeat the cascade on purpose
    res = outbound.dispatch_due(session, now=msg.scheduled_for + timedelta(minutes=1))
    assert res[0]["status"] == "blocked" and sms.sent == []


def test_daily_caps_defer(session, lead, sms, monkeypatch):
    monkeypatch.setattr(outbound, "utcnow", lambda: TUE_NOON)
    monkeypatch.setattr(get_settings(), "max_sms_per_lead_per_day", 1)
    for i in range(2):
        m = outbound.draft_message(session, lead, "sms", f"Hello number {i}", created_by="agent:seller")
        approve(session, m)
    assert len(sms.sent) == 1 and m.status == MessageStatus.SCHEDULED and m.scheduled_for > TUE_NOON


def test_provider_failure_marks_failed(session, lead, sms, monkeypatch):
    monkeypatch.setattr(outbound, "utcnow", lambda: TUE_NOON)
    sms.error = PermanentSendError("bad number")
    msg = outbound.draft_message(session, lead, "sms", "Hi Jane.", created_by="agent:seller")
    a = approve(session, msg)
    assert a.status == ApprovalStatus.FAILED and msg.status == MessageStatus.FAILED and "bad number" in msg.error
    sms.error = TransientError("503")
    m2 = outbound.draft_message(session, lead, "sms", "Hi again Jane.", created_by="agent:seller")
    assert approve(session, m2).status == ApprovalStatus.FAILED


def test_reject_marks_message_rejected(session, lead, sms):
    msg = outbound.draft_message(session, lead, "sms", "Hi Jane.", created_by="agent:seller")
    approvals.decide(session, msg.approval_id, approve=False, user_label="user:9", user_role=Role.MANAGER)
    assert msg.status == MessageStatus.REJECTED and sms.sent == []


def test_edited_body_after_approval_request_is_refused(session, lead, sms, monkeypatch):
    monkeypatch.setattr(outbound, "utcnow", lambda: TUE_NOON)
    msg = outbound.draft_message(session, lead, "sms", "Hi Jane.", created_by="agent:seller")
    msg.body = "something else entirely"
    session.flush()
    assert approve(session, msg).status == ApprovalStatus.FAILED and sms.sent == []


def test_email_draft_and_send(session, lead, monkeypatch):
    sent = []

    class FakeEmail:
        def send(self, to, subject, body):
            sent.append((to, subject))
            return SendResult("g1")

    lead.seller.email = "jane@example.com"
    registry.configure(email=FakeEmail())
    monkeypatch.setattr(outbound, "utcnow", lambda: TUE_NOON)
    msg = outbound.draft_message(session, lead, "email", "Hello Jane, wanted to follow up.", subject="Your property", created_by="agent:seller")
    approve(session, msg)
    assert sent == [("jane@example.com", "Your property")]
    registry.reset()


def test_inbound_reply_creates_conversation_task_and_dedupes(session, lead):
    r = inbound.handle_inbound_sms(session, "+15128675309", "Yes I need to sell fast, house needs a new roof", "SM_x")
    assert r.action == "message" and r.lead_id == lead.id and lead.status == LeadStatus.ENGAGED
    assert inbound.handle_inbound_sms(session, "+15128675309", "Yes", "SM_x").action == "duplicate"
    assert inbound.handle_inbound_sms(session, "garbage", "hi").action == "ignored"
    assert inbound.handle_inbound_sms(session, "+15128675309", "help").reply
    conv = session.query(Conversation).one()
    assert len(conv.messages) == 1 and conv.messages[0].body.startswith("Yes I need")


def test_unknown_texter_becomes_lead(session):
    r = inbound.handle_inbound_sms(session, "+14155552671", "I heard you buy houses", "SM_new")
    assert r.action == "message" and r.lead_id
