import re
from typing import Any

from sqlalchemy.orm import Session

from bravo.enums import ActorType
from bravo.models import ActivityLog

_SENSITIVE_KEYS = {"phone", "email", "to", "body", "name", "mailing_address", "token", "password", "secret"}
_PHONE_RE = re.compile(r"\+?\d[\d\-\s().]{8,}\d")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _scrub_text(text: str) -> str:
    text = _EMAIL_RE.sub("[email]", text)
    return _PHONE_RE.sub("[phone]", text)


def redact(value: Any, key: str = "") -> Any:
    """Keep the audit log useful without turning it into a second copy of PII."""
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, key) for v in value]
    if isinstance(value, str):
        if key.lower() in _SENSITIVE_KEYS:
            return f"[redacted:{len(value)} chars]"
        return _scrub_text(value)[:2000]
    return value


def log_event(
    session: Session,
    *,
    actor: str,
    event: str,
    actor_type: ActorType = ActorType.SYSTEM,
    entity_type: str | None = None,
    entity_id: int | None = None,
    data: dict | None = None,
) -> ActivityLog:
    row = ActivityLog(
        actor_type=str(actor_type),
        actor=actor,
        event=event,
        entity_type=entity_type,
        entity_id=entity_id,
        data=redact(data or {}),
    )
    session.add(row)
    session.flush()
    return row
