from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class CRMContact:
    local_id: int
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    external_id: str | None = None


@dataclass
class CRMDeal:
    local_id: int
    title: str
    stage: str
    contact_external_id: str | None = None
    external_id: str | None = None
    properties: dict = field(default_factory=dict)


class CRMAdapter(ABC):
    """Everything Jarvis needs from a CRM. Add a provider by implementing these five methods."""

    name: str

    @abstractmethod
    def upsert_contact(self, contact: CRMContact) -> str: ...

    @abstractmethod
    def upsert_deal(self, deal: CRMDeal) -> str: ...

    @abstractmethod
    def add_note(self, external_id: str, note: str) -> None: ...

    @abstractmethod
    def update_stage(self, deal_external_id: str, stage: str) -> None: ...

    @abstractmethod
    def fetch_leads(self, limit: int = 100) -> list[dict]:
        """Return raw lead dicts (name, phone, email, address fields...) for the intake agent."""
