"""Adapter for the Bravo Vision lead generator app.

Wire contract (Bravo Vision implements the receiving side; see README "Bravo Vision sync"):
  POST {base}/api/jarvis/events   body: {"event": "contact_upsert|deal_upsert|note|stage", ...}  -> {"id": "<bravo id>"}
  GET  {base}/api/jarvis/leads?limit=N                                                            -> {"leads": [ ... ]}
Every request carries `Authorization: Bearer <BV_BRAVO_API_KEY>` and `X-Jarvis-Signature: sha256=<HMAC of the body, key BV_BRAVO_SYNC_KEY>`.
"""
import hashlib
import hmac
import json

import httpx

from bravo.config import get_settings
from bravo.integrations.base import IntegrationNotConfigured, PermanentSendError
from bravo.integrations.crm.base import CRMAdapter, CRMContact, CRMDeal
from bravo.retry import TransientError, with_retry


def sign(body: bytes, key: str) -> str:
    return "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


class BravoVisionCRM(CRMAdapter):
    name = "bravo"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, sync_key: str | None = None,
                 transport: httpx.BaseTransport | None = None, sleep=None):
        s = get_settings()
        self.base = (base_url or s.bravo_base_url).rstrip("/")
        self.api_key, self.sync_key = api_key or s.bravo_api_key, sync_key or s.bravo_sync_key
        if not (self.base and self.api_key and self.sync_key):
            raise IntegrationNotConfigured("BV_BRAVO_BASE_URL, BV_BRAVO_API_KEY and BV_BRAVO_SYNC_KEY are required")
        self.http = httpx.Client(base_url=self.base, timeout=15, transport=transport)
        self._retry = {"sleep": sleep} if sleep else {}

    def _req(self, method: str, path: str, payload: dict | None = None, params: dict | None = None) -> dict:
        body = json.dumps(payload, default=str, sort_keys=True).encode() if payload is not None else b""
        headers = {"Authorization": f"Bearer {self.api_key}", "X-Jarvis-Signature": sign(body, self.sync_key), "Content-Type": "application/json"}

        def _once():
            try:
                r = self.http.request(method, path, content=body or None, params=params, headers=headers)
            except httpx.TransportError as exc:
                raise TransientError(str(exc)) from exc
            if r.status_code == 429 or r.status_code >= 500:
                raise TransientError(f"Bravo Vision {r.status_code}")
            if r.status_code >= 400:
                raise PermanentSendError(f"Bravo Vision rejected request ({r.status_code})")
            return r.json() if r.content else {}

        return with_retry(_once, **self._retry)

    def upsert_contact(self, c: CRMContact) -> str:
        res = self._req("POST", "/api/jarvis/events", {"event": "contact_upsert", "jarvis_id": c.local_id, "external_id": c.external_id,
                                                        "name": c.name, "email": c.email, "phone": c.phone})
        return str(res.get("id") or c.external_id or f"jarvis-{c.local_id}")

    def upsert_deal(self, d: CRMDeal) -> str:
        res = self._req("POST", "/api/jarvis/events", {"event": "deal_upsert", "jarvis_id": d.local_id, "external_id": d.external_id,
                                                        "title": d.title, "stage": d.stage, "contact_external_id": d.contact_external_id,
                                                        "properties": d.properties})
        return str(res.get("id") or d.external_id or f"jarvis-deal-{d.local_id}")

    def add_note(self, external_id: str, note: str) -> None:
        self._req("POST", "/api/jarvis/events", {"event": "note", "external_id": external_id, "note": note})

    def update_stage(self, deal_external_id: str, stage: str) -> None:
        self._req("POST", "/api/jarvis/events", {"event": "stage", "external_id": deal_external_id, "stage": stage})

    def fetch_leads(self, limit: int = 100) -> list[dict]:
        return list(self._req("GET", "/api/jarvis/leads", params={"limit": limit}).get("leads", []))
