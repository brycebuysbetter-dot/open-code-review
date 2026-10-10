"""The human-approval gate.

Agents can only *request* approvals. Execution happens here, after a human with
the right permission decides, and only for exactly the payload that was reviewed.
"""
import hashlib
import json
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from bravo import audit
from bravo.config import get_settings
from bravo.db import utcnow
from bravo.enums import ActorType, ApprovalKind, ApprovalStatus
from bravo.models import Approval
from bravo.rbac import NO_SELF_APPROVAL, has_permission


class ApprovalError(Exception):
    pass


class PermissionDenied(ApprovalError):
    pass


Handler = Callable[[Session, Approval], dict]
_handlers: dict[str, Handler] = {}


def register_handler(kind: ApprovalKind, fn: Handler) -> None:
    _handlers[str(kind)] = fn


def payload_digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def request_approval(
    session: Session,
    *,
    kind: ApprovalKind,
    summary: str,
    payload: dict,
    requested_by: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    actor_type: ActorType = ActorType.AGENT,
) -> Approval:
    settings = get_settings()
    approval = Approval(
        kind=str(kind),
        summary=summary[:500],
        payload=payload,
        payload_hash=payload_digest(payload),
        entity_type=entity_type,
        entity_id=entity_id,
        requested_by=requested_by,
        expires_at=utcnow() + timedelta(hours=settings.approval_ttl_hours),
    )
    session.add(approval)
    session.flush()
    audit.log_event(
        session, actor=requested_by, actor_type=actor_type, event="approval.requested",
        entity_type="approval", entity_id=approval.id,
        data={"kind": str(kind), "summary": summary, "target": [entity_type, entity_id]},
    )
    return approval


def _check_can_decide(approval: Approval, user_role: str, user_label: str) -> None:
    if not has_permission(user_role, f"approve:{approval.kind}"):
        raise PermissionDenied(f"role '{user_role}' may not decide '{approval.kind}' approvals")
    if ApprovalKind(approval.kind) in NO_SELF_APPROVAL and approval.requested_by == user_label:
        raise PermissionDenied("requester cannot approve their own request")


def decide(
    session: Session, approval_id: int, *, approve: bool, user_label: str, user_role: str, note: str | None = None
) -> Approval:
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise ApprovalError("approval not found")
    _check_can_decide(approval, user_role, user_label)
    if approval.expires_at and approval.expires_at < utcnow() and approval.status == ApprovalStatus.PENDING:
        approval.status = ApprovalStatus.EXPIRED
        audit.log_event(session, actor="system", event="approval.expired", entity_type="approval", entity_id=approval.id)
        raise ApprovalError("approval expired")

    new_status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
    # Conditional UPDATE: two concurrent deciders cannot both win.
    claimed = session.execute(
        update(Approval)
        .where(Approval.id == approval_id, Approval.status == ApprovalStatus.PENDING)
        .values(status=new_status, decided_by=user_label, decided_at=utcnow(), decision_note=note)
    )
    if claimed.rowcount != 1:
        raise ApprovalError(f"approval is already {approval.status}")
    session.refresh(approval)
    audit.log_event(
        session, actor=user_label, actor_type=ActorType.USER,
        event="approval.approved" if approve else "approval.rejected",
        entity_type="approval", entity_id=approval.id, data={"kind": approval.kind, "note": note},
    )
    if approve:
        _execute(session, approval)
    else:
        _on_reject(session, approval)
    return approval


def _execute(session: Session, approval: Approval) -> None:
    handler = _handlers.get(approval.kind)
    if handler is None:
        approval.status = ApprovalStatus.FAILED
        approval.result = {"error": f"no executor registered for '{approval.kind}'"}
        audit.log_event(session, actor="system", event="approval.failed", entity_type="approval",
                        entity_id=approval.id, data=approval.result)
        return
    if payload_digest(approval.payload) != approval.payload_hash:
        approval.status = ApprovalStatus.FAILED
        approval.result = {"error": "payload changed after review; refusing to execute"}
        audit.log_event(session, actor="system", event="approval.tamper_detected", entity_type="approval",
                        entity_id=approval.id)
        return
    try:
        with session.begin_nested():
            result = handler(session, approval)
        approval.result = result
        if result.get("error"):
            approval.status = ApprovalStatus.FAILED
            audit.log_event(session, actor="system", event="approval.failed", entity_type="approval",
                            entity_id=approval.id, data=result)
            return
        approval.status = ApprovalStatus.EXECUTED
        approval.executed_at = utcnow()
        audit.log_event(session, actor="system", event="approval.executed", entity_type="approval",
                        entity_id=approval.id, data={"kind": approval.kind, "result": result})
    except Exception as exc:  # executor failures must not lose the decision record
        approval.status = ApprovalStatus.FAILED
        approval.result = {"error": str(exc)[:500]}
        audit.log_event(session, actor="system", event="approval.failed", entity_type="approval",
                        entity_id=approval.id, data=approval.result)


_reject_hooks: dict[str, Handler] = {}


def register_reject_hook(kind: ApprovalKind, fn: Handler) -> None:
    _reject_hooks[str(kind)] = fn


def _on_reject(session: Session, approval: Approval) -> None:
    hook = _reject_hooks.get(approval.kind)
    if hook:
        hook(session, approval)
