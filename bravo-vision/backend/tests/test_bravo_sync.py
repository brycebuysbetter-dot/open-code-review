import pytest
from fastapi.testclient import TestClient

from bravo import db
from bravo.api import deps
from bravo.config import get_settings
from bravo.main import create_app
from bravo.models import ActivityLog, Lead, Seller
from bravo.services import bravo_sync, leads


@pytest.fixture
def client(monkeypatch):
    db.init_engine("sqlite://")
    db.create_all()
    monkeypatch.setattr(get_settings(), "bravo_sync_key", "shared-sync-key")
    with TestClient(create_app()) as c:
        yield c
    db.Base.metadata.drop_all(db.get_engine())


H = {"X-Sync-Key": "shared-sync-key"}
ITEM = {"external_id": "BV-100", "name": "Jane Doe", "phone": "512-867-5309", "street": "12 Oak Lane", "city": "Austin", "state": "TX",
        "zip": "78701", "timeline": "asap", "reason": "foreclosure", "sms_consent": True, "consent_source": "Bravo Vision form 77"}


def test_sync_requires_key(client):
    assert client.post("/api/sync/bravo/leads", json={"leads": [ITEM]}).status_code == 401
    assert client.post("/api/sync/bravo/leads", json={"leads": [ITEM]}, headers={"X-Sync-Key": "nope"}).status_code == 401
    assert client.get("/api/sync/bravo/leads").status_code == 401


def test_sync_not_configured_rejects_everything(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "bravo_sync_key", "")
    assert client.post("/api/sync/bravo/leads", json={"leads": [ITEM]}, headers={"X-Sync-Key": ""}).status_code == 401


def test_push_is_idempotent_and_carries_consent(client):
    r = client.post("/api/sync/bravo/leads", json={"leads": [ITEM]}, headers=H).json()["results"][0]
    assert r["created"] and r["external_id"] == "BV-100"
    again = client.post("/api/sync/bravo/leads", json={"leads": [{**ITEM, "notes": "called twice, wants a quick close"}]}, headers=H).json()["results"][0]
    assert not again["created"] and again["lead_id"] == r["lead_id"] and again["duplicate_of_existing"]
    with db.session_scope() as s:
        assert s.query(Lead).count() == 1 and s.query(Seller).count() == 1
        lead = s.get(Lead, r["lead_id"])
        assert lead.crm_id == "BV-100" and lead.source == "bravo_vision" and lead.sms_consent and lead.consent_source == "Bravo Vision form 77"
        assert not lead.voice_consent
        assert any(e.event == "sync.bravo_lead_upserted" for e in s.query(ActivityLog))


def test_batch_reports_per_item_errors_and_validates(client):
    body = {"leads": [{"name": "no id", "phone": "512-867-5309"}, {"external_id": "BV-2"}, {**ITEM, "external_id": "BV-3"}]}
    res = client.post("/api/sync/bravo/leads", json=body, headers=H).json()["results"]
    assert res[0]["error"] == "external_id is required" and "error" in res[1] and res[2]["created"]
    assert client.post("/api/sync/bravo/leads", json={"leads": []}, headers=H).status_code == 422


def test_pull_feed_has_status_but_no_pii(client):
    client.post("/api/sync/bravo/leads", json={"leads": [ITEM]}, headers=H)
    feed = client.get("/api/sync/bravo/leads", headers=H).json()["leads"]
    assert feed[0]["external_id"] == "BV-100" and feed[0]["motivation_score"] > 0 and feed[0]["sms_consent"] is True
    dumped = str(feed)
    assert "5128675309" not in dumped and "Jane" not in dumped and "Oak" not in dumped
    assert client.get("/api/sync/bravo/leads?since=2999-01-01T00:00:00", headers=H).json()["leads"] == []


def test_service_level_upsert_without_api(session):
    r = bravo_sync.upsert_lead(session, {"external_id": "X1", "phone": "512-867-5309", "street": "9 Elm St", "zip": "78701"})
    assert r["created"]
    other = leads.ingest_lead(session, {"phone": "415-867-5309", "street": "9 Elm St", "zip": "78701"}, source="csv")
    assert other.duplicate_of.crm_id == "X1"  # same property: deduped against the synced lead
