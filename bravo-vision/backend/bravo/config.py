from functools import lru_cache

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BV_", env_file=".env", extra="ignore", populate_by_name=True)

    env: str = "development"
    database_url: str = Field(default="sqlite:///./bravo.db", validation_alias=AliasChoices("BV_DATABASE_URL", "DATABASE_URL"))
    encryption_keys: str = ""
    public_base_url: str = Field(default="", validation_alias=AliasChoices("BV_PUBLIC_BASE_URL", "PUBLIC_BASE_URL"))
    cors_origins: str = "http://localhost:5173"

    business_tz: str = Field(default="America/Chicago", validation_alias=AliasChoices("BV_BUSINESS_TZ", "APP_TIMEZONE"))
    contact_start_hour: int = 9
    contact_end_hour: int = 19
    contact_days: str = "0,1,2,3,4,5"
    default_discount_factor: float = 0.70
    hot_lead_threshold: int = 70
    max_sms_per_day: int = 200
    max_sms_per_lead_per_day: int = 2
    api_rate_limit_per_minute: int = Field(default=120, validation_alias=AliasChoices("BV_API_RATE_LIMIT_PER_MINUTE", "RATE_LIMIT_PER_MINUTE"))
    approval_ttl_hours: int = 72

    anthropic_api_key: str = Field(default="", validation_alias=AliasChoices("BV_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"))
    claude_model: str = Field(default="claude-sonnet-5-5", validation_alias=AliasChoices("BV_CLAUDE_MODEL", "ANTHROPIC_MODEL"))
    agent_max_steps: int = 8

    twilio_account_sid: str = Field(default="", validation_alias=AliasChoices("BV_TWILIO_ACCOUNT_SID", "TWILIO_ACCOUNT_SID"))
    twilio_auth_token: str = Field(default="", validation_alias=AliasChoices("BV_TWILIO_AUTH_TOKEN", "TWILIO_AUTH_TOKEN"))
    twilio_from_number: str = Field(default="", validation_alias=AliasChoices("BV_TWILIO_FROM_NUMBER", "TWILIO_FROM_NUMBER"))

    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_calendar_id: str = "primary"
    business_email: str = ""

    app_secret_key: str = Field(default="", validation_alias=AliasChoices("BV_APP_SECRET_KEY", "APP_SECRET_KEY"))
    quiet_hours_start: str = Field(default="", validation_alias=AliasChoices("BV_QUIET_HOURS_START", "QUIET_HOURS_START"))
    quiet_hours_end: str = Field(default="", validation_alias=AliasChoices("BV_QUIET_HOURS_END", "QUIET_HOURS_END"))
    automatic_messaging_enabled: bool = Field(default=True, validation_alias=AliasChoices("BV_AUTOMATIC_MESSAGING_ENABLED", "AUTOMATIC_MESSAGING_ENABLED"))
    automatic_voice_enabled: bool = Field(default=True, validation_alias=AliasChoices("BV_AUTOMATIC_VOICE_ENABLED", "AUTOMATIC_VOICE_ENABLED"))
    voice_max_duration_seconds: int = Field(default=600, validation_alias=AliasChoices("BV_VOICE_MAX_DURATION_SECONDS", "VOICE_MAX_DURATION_SECONDS"))
    voice_max_turns: int = 10
    require_consent: bool | None = None  # None: required in production, optional elsewhere
    bravo_base_url: str = ""
    bravo_api_key: str = ""
    bravo_sync_key: str = ""

    owner_name: str = Field(default="Bryce", validation_alias=AliasChoices("BV_OWNER_NAME", "OWNER_NAME"))
    business_name: str = "Bravo Vision"
    business_address: str = ""

    webform_key: str = ""

    crm_provider: str = "local"
    hubspot_token: str = ""

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def consent_required(self) -> bool:
        return self.is_production if self.require_consent is None else self.require_consent

    @property
    def contact_weekdays(self) -> set[int]:
        return {int(d) for d in self.contact_days.split(",") if d.strip() != ""}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @model_validator(mode="after")
    def _decrypt_secrets(self):
        """Secrets may be stored as `enc:<token>` (see `bravo encrypt-secret`) so env files and CI logs hold no plaintext keys."""
        names = ("anthropic_api_key", "twilio_auth_token", "google_client_secret", "google_refresh_token", "hubspot_token", "webform_key",
                 "bravo_api_key", "bravo_sync_key")
        if not any(getattr(self, n).startswith("enc:") for n in names):
            return self
        from bravo.crypto import Crypto

        from bravo.crypto import derive_key

        keys = [k.strip() for k in self.encryption_keys.split(",") if k.strip()]
        if not keys and self.app_secret_key:
            keys = [derive_key(self.app_secret_key)]
        if not keys:
            raise ValueError("BV_ENCRYPTION_KEYS (or APP_SECRET_KEY) is required to decrypt enc: secrets")
        crypto = Crypto(keys)
        for n in names:
            v = getattr(self, n)
            if v.startswith("enc:"):
                object.__setattr__(self, n, crypto.decrypt(v[4:]))
        return self

    @model_validator(mode="after")
    def _production_guards(self):
        if self.is_production:
            if not self.encryption_keys and not self.app_secret_key:
                raise ValueError("BV_ENCRYPTION_KEYS or APP_SECRET_KEY is required in production")
            if self.database_url.startswith("sqlite"):
                raise ValueError("production requires PostgreSQL (BV_DATABASE_URL)")
        for attr, target in (("quiet_hours_end", "contact_start_hour"), ("quiet_hours_start", "contact_end_hour")):
            raw = getattr(self, attr)
            if raw:
                hh, _, mm = raw.partition(":")
                if not hh.isdigit() or (mm and mm != "00"):
                    raise ValueError(f"{attr} must look like 09:00 (whole hours)")
                object.__setattr__(self, target, int(hh) or (24 if target == "contact_end_hour" else 0))
        if not 0 <= self.contact_start_hour < self.contact_end_hour <= 24:
            raise ValueError("invalid contact hours")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
