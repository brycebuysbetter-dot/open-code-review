"""Field-level encryption and blind indexes for PII.

Sensitive columns are Fernet-encrypted at rest. Equality lookups (duplicate
detection, opt-out checks) use an HMAC blind index, so plaintext never needs to
be searchable in the database.
"""
import base64
import hashlib
import hmac
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from bravo.config import get_settings


def derive_key(secret: str) -> str:
    """Fernet key derived from APP_SECRET_KEY, for deployments that configure a single secret."""
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"bravo-fernet").derive(secret.encode())
    return base64.urlsafe_b64encode(raw).decode()


class Crypto:
    def __init__(self, keys: list[str]):
        if not keys:
            raise ValueError("at least one encryption key is required")
        self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])
        self._index_key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=b"bravo-blind-index"
        ).derive(keys[0].encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("unable to decrypt value (wrong or rotated-out key)") from exc

    def blind_index(self, value: str) -> str:
        return hmac.new(self._index_key, value.encode(), hashlib.sha256).hexdigest()


@lru_cache
def get_crypto() -> Crypto:
    settings = get_settings()
    keys = [k.strip() for k in settings.encryption_keys.split(",") if k.strip()]
    if not keys and settings.app_secret_key:
        keys = [derive_key(settings.app_secret_key)]
    if not keys:
        if settings.is_production:
            raise RuntimeError("BV_ENCRYPTION_KEYS or APP_SECRET_KEY is required in production")
        # Development convenience only: an ephemeral key means data does not survive restarts.
        keys = [Fernet.generate_key().decode()]
    return Crypto(keys)


class EncryptedString(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else get_crypto().encrypt(value)

    def process_result_value(self, value, dialect):
        return None if value is None else get_crypto().decrypt(value)


def mask_phone(phone: str | None) -> str:
    if not phone:
        return ""
    return "***" + phone[-4:] if len(phone) > 4 else "***"


def mask_email(email: str | None) -> str:
    if not email or "@" not in email:
        return ""
    local, domain = email.split("@", 1)
    return f"{local[:1]}***@{domain}"
