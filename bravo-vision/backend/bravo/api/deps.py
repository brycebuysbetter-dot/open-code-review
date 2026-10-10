import hashlib
import secrets

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from bravo.config import get_settings
from bravo.db import get_session
from bravo.models import User
from bravo.ratelimit import SlidingWindowLimiter
from bravo.rbac import has_permission

_api_limiter: SlidingWindowLimiter | None = None
_agent_limiter: SlidingWindowLimiter | None = None
_public_limiter: SlidingWindowLimiter | None = None


def reset_limiters() -> None:
    global _api_limiter, _agent_limiter, _public_limiter
    s = get_settings()
    _api_limiter = SlidingWindowLimiter(s.api_rate_limit_per_minute)
    _agent_limiter = SlidingWindowLimiter(max(1, s.api_rate_limit_per_minute // 6))
    _public_limiter = SlidingWindowLimiter(30)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return "bv_" + secrets.token_urlsafe(32)


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def current_user(request: Request, authorization: str | None = Header(default=None), session: Session = Depends(get_session)) -> User:
    if _api_limiter is None:
        reset_limiters()
    if not authorization or not authorization.lower().startswith("bearer "):
        if not _api_limiter.allow("anon:" + _client_key(request)):
            raise HTTPException(429, "rate limit exceeded")
        raise HTTPException(401, "missing bearer token")
    token = authorization[7:].strip()
    user = session.scalar(select(User).where(User.token_hash == hash_token(token), User.active.is_(True)))
    if user is None:
        if not _api_limiter.allow("anon:" + _client_key(request)):
            raise HTTPException(429, "rate limit exceeded")
        raise HTTPException(401, "invalid token")
    if not _api_limiter.allow(f"user:{user.id}"):
        raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})
    return user


def require(permission: str, *, expensive: bool = False):
    def dep(user: User = Depends(current_user)) -> User:
        if not has_permission(user.role, permission):
            raise HTTPException(403, f"missing permission: {permission}")
        if expensive and not _agent_limiter.allow(f"agent:{user.id}"):
            raise HTTPException(429, "agent rate limit exceeded", headers={"Retry-After": "60"})
        return user

    return dep


def public_rate_limit(request: Request) -> None:
    if _public_limiter is None:
        reset_limiters()
    if not _public_limiter.allow("pub:" + _client_key(request)):
        raise HTTPException(429, "rate limit exceeded")


def label(user: User) -> str:
    return f"user:{user.id}"


def safe_compare(a: str, b: str) -> bool:
    return bool(a) and bool(b) and secrets.compare_digest(a.encode(), b.encode())
