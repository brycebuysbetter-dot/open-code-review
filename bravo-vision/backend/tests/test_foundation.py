from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from bravo import approvals, audit
from bravo.crypto import Crypto, mask_email, mask_phone
from bravo.db import utcnow
from bravo.enums import ApprovalKind, ApprovalStatus, Role
from bravo.models import ActivityLog, Approval, Seller
from bravo.rbac import has_permission
from bravo.ratelimit import SlidingWindowLimiter
from bravo.retry import TransientError, with_retry


def test_pii_encrypted_at_rest(session):
    s = Seller(name="Jane Doe", phone="+15125550123", phone_hash="h")
    session.add(s)
    session.commit()
    raw = session.execute(text("select name, phone from sellers")).one()
    assert "Jane" not in raw[0] and "5125550123" not in raw[1]
    session.expire_all()
    assert session.get(Seller, s.id).phone == "+15125550123"


def test_key_rotation_and_blind_index():
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    token = Crypto([old]).encrypt("secret")
    assert Crypto([new, old]).decrypt(token) == "secret"
    with pytest.raises(ValueError):
        Crypto([new]).decrypt(token)
    c = Crypto([old])
    assert c.blind_index("a") == c.blind_index("a") != c.blind_index("b")


def test_masks():
    assert mask_phone("+15125550123") == "***0123"
    assert mask_email("jane@x.com") == "j***@x.com"


def test_audit_redacts_pii_and_is_append_only(session):
    row = audit.log_event(session, actor="a", event="e", data={"phone": "+15125550123", "note": "call 512-555-0123 or j@x.com"})
    assert "5125550123" not in str(row.data) and "512-555-0123" not in str(row.data) and "j@x.com" not in str(row.data)
    session.commit()
    row.event = "changed"
    with pytest.raises(RuntimeError):
        session.commit()
    session.rollback()
    session.delete(session.get(ActivityLog, row.id))
    with pytest.raises(RuntimeError):
        session.commit()


def test_rbac_matrix():
    assert has_permission(Role.ADMIN, "approve:payment")
    assert not has_permission(Role.MANAGER, "approve:contract")
    assert not has_permission(Role.VIEWER, "leads:write")
    assert not has_permission("nonsense", "leads:read")


def _req(session, kind=ApprovalKind.OFFER, by="agent:test", payload=None):
    return approvals.request_approval(session, kind=kind, summary="s", payload=payload or {"x": 1}, requested_by=by)


def test_approval_executes_once(session):
    calls = []
    approvals.register_handler(ApprovalKind.OFFER, lambda s, a: calls.append(a.id) or {"ok": True})
    a = _req(session)
    approvals.decide(session, a.id, approve=True, user_label="user:1", user_role=Role.MANAGER)
    assert a.status == ApprovalStatus.EXECUTED and calls == [a.id]
    with pytest.raises(approvals.ApprovalError):
        approvals.decide(session, a.id, approve=True, user_label="user:1", user_role=Role.MANAGER)
    assert calls == [a.id]


def test_approval_permissions_and_self_approval(session):
    approvals.register_handler(ApprovalKind.CONTRACT, lambda s, a: {})
    a = _req(session, ApprovalKind.CONTRACT, by="user:5")
    with pytest.raises(approvals.PermissionDenied):
        approvals.decide(session, a.id, approve=True, user_label="user:6", user_role=Role.MANAGER)
    with pytest.raises(approvals.PermissionDenied):
        approvals.decide(session, a.id, approve=True, user_label="user:5", user_role=Role.ADMIN)
    approvals.decide(session, a.id, approve=True, user_label="user:6", user_role=Role.ADMIN)
    assert a.status == ApprovalStatus.EXECUTED


def test_reject_does_not_execute(session):
    calls = []
    approvals.register_handler(ApprovalKind.MESSAGE, lambda s, a: calls.append(1) or {})
    a = _req(session, ApprovalKind.MESSAGE)
    approvals.decide(session, a.id, approve=False, user_label="user:1", user_role=Role.ADMIN, note="no")
    assert a.status == ApprovalStatus.REJECTED and not calls


def test_tampered_payload_refused(session):
    approvals.register_handler(ApprovalKind.OFFER, lambda s, a: {})
    a = _req(session)
    a.payload = {"x": 999}
    session.flush()
    approvals.decide(session, a.id, approve=True, user_label="user:1", user_role=Role.ADMIN)
    assert a.status == ApprovalStatus.FAILED and "changed" in a.result["error"]


def test_failed_handler_recorded(session):
    def boom(s, a):
        raise RuntimeError("twilio down")

    approvals.register_handler(ApprovalKind.OFFER, boom)
    a = _req(session)
    approvals.decide(session, a.id, approve=True, user_label="user:1", user_role=Role.ADMIN)
    assert a.status == ApprovalStatus.FAILED and "twilio" in a.result["error"]


def test_expired_approval(session):
    a = _req(session)
    a.expires_at = utcnow() - timedelta(hours=1)
    session.flush()
    with pytest.raises(approvals.ApprovalError):
        approvals.decide(session, a.id, approve=True, user_label="u", user_role=Role.ADMIN)
    assert session.get(Approval, a.id).status == ApprovalStatus.EXPIRED


def test_retry_backoff():
    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("503")
        return "ok"

    assert with_retry(flaky, sleep=sleeps.append) == "ok" and len(sleeps) == 2
    with pytest.raises(TransientError):
        with_retry(lambda: (_ for _ in ()).throw(TransientError("x")), attempts=2, sleep=lambda s: None)
    with pytest.raises(ValueError):  # non-transient errors are not retried
        with_retry(lambda: (_ for _ in ()).throw(ValueError("x")), sleep=lambda s: None)


def test_rate_limiter_window():
    now = [0.0]
    lim = SlidingWindowLimiter(2, 10, clock=lambda: now[0])
    assert lim.allow("k") and lim.allow("k") and not lim.allow("k") and lim.allow("other")
    now[0] = 11
    assert lim.allow("k")
