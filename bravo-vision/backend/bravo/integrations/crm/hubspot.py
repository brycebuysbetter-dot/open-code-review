"""HubSpot CRM v3 adapter. Verify property names against your portal before enabling in production."""
import httpx

from bravo.config import get_settings
from bravo.integrations.base import IntegrationNotConfigured, PermanentSendError
from bravo.integrations.crm.base import CRMAdapter, CRMContact, CRMDeal
from bravo.retry import TransientError, with_retry

BASE = "https://api.hubapi.com"
NOTE_TO_CONTACT = 202
NOTE_TO_DEAL = 214


class HubSpotCRM(CRMAdapter):
    name = "hubspot"

    def __init__(self, token: str | None = None, transport: httpx.BaseTransport | None = None, sleep=None):
        token = token or get_settings().hubspot_token
        if not token:
            raise IntegrationNotConfigured("BV_HUBSPOT_TOKEN is not configured")
        self.http = httpx.Client(
            base_url=BASE, headers={"Authorization": f"Bearer {token}"}, timeout=15, transport=transport
        )
        self._retry = {"sleep": sleep} if sleep else {}

    def _req(self, method: str, url: str, **kw) -> dict:
        def _once():
            try:
                r = self.http.request(method, url, **kw)
            except httpx.TransportError as exc:
                raise TransientError(str(exc)) from exc
            if r.status_code == 429 or r.status_code >= 500:
                raise TransientError(f"HubSpot {r.status_code}")
            if r.status_code >= 400:
                raise PermanentSendError(f"HubSpot rejected request ({r.status_code})")
            return r.json() if r.content else {}

        return with_retry(_once, **self._retry)

    def upsert_contact(self, c: CRMContact) -> str:
        props = {k: v for k, v in {"email": c.email, "phone": c.phone, "firstname": c.name}.items() if v}
        if c.external_id:
            self._req("PATCH", f"/crm/v3/objects/contacts/{c.external_id}", json={"properties": props})
            return c.external_id
        if c.email:
            found = self._req("POST", "/crm/v3/objects/contacts/search", json={
                "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": c.email}]}], "limit": 1})
            if found.get("results"):
                cid = found["results"][0]["id"]
                self._req("PATCH", f"/crm/v3/objects/contacts/{cid}", json={"properties": props})
                return cid
        return self._req("POST", "/crm/v3/objects/contacts", json={"properties": props})["id"]

    def upsert_deal(self, d: CRMDeal) -> str:
        props = {"dealname": d.title, "dealstage": d.stage, **d.properties}
        if d.external_id:
            self._req("PATCH", f"/crm/v3/objects/deals/{d.external_id}", json={"properties": props})
            return d.external_id
        body: dict = {"properties": props}
        if d.contact_external_id:
            body["associations"] = [{"to": {"id": d.contact_external_id},
                                     "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 3}]}]
        return self._req("POST", "/crm/v3/objects/deals", json=body)["id"]

    def add_note(self, external_id: str, note: str) -> None:
        self._req("POST", "/crm/v3/objects/notes", json={
            "properties": {"hs_note_body": note, "hs_timestamp": _now_iso()},
            "associations": [{"to": {"id": external_id},
                              "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": NOTE_TO_CONTACT}]}],
        })

    def update_stage(self, deal_external_id: str, stage: str) -> None:
        self._req("PATCH", f"/crm/v3/objects/deals/{deal_external_id}", json={"properties": {"dealstage": stage}})

    def fetch_leads(self, limit: int = 100) -> list[dict]:
        res = self._req("GET", "/crm/v3/objects/contacts", params={
            "limit": limit, "properties": "firstname,lastname,email,phone,address,city,state,zip"})
        out = []
        for r in res.get("results", []):
            p = r.get("properties", {})
            out.append({"name": " ".join(x for x in (p.get("firstname"), p.get("lastname")) if x), "email": p.get("email"),
                        "phone": p.get("phone"), "street": p.get("address"), "city": p.get("city"),
                        "state": p.get("state"), "zip": p.get("zip"), "crm_id": r["id"]})
        return out


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
