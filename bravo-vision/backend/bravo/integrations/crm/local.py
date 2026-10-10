from bravo.integrations.crm.base import CRMAdapter, CRMContact, CRMDeal


class LocalCRM(CRMAdapter):
    """The Jarvis database is the CRM. Used until an external CRM is wired in."""

    name = "local"

    def upsert_contact(self, contact: CRMContact) -> str:
        return contact.external_id or f"local-contact-{contact.local_id}"

    def upsert_deal(self, deal: CRMDeal) -> str:
        return deal.external_id or f"local-deal-{deal.local_id}"

    def add_note(self, external_id: str, note: str) -> None:
        return None

    def update_stage(self, deal_external_id: str, stage: str) -> None:
        return None

    def fetch_leads(self, limit: int = 100) -> list[dict]:
        return []
