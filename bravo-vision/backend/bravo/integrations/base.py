from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class IntegrationNotConfigured(RuntimeError):
    pass


class PermanentSendError(RuntimeError):
    """The provider rejected the request; retrying will not help."""


@dataclass
class SendResult:
    external_id: str


@dataclass
class InboundEmail:
    external_id: str
    sender: str
    subject: str
    body: str


class SMSGateway(Protocol):
    def send(self, to: str, body: str) -> SendResult: ...


class EmailGateway(Protocol):
    def send(self, to: str, subject: str, body: str) -> SendResult: ...
    def fetch_inbound(self, query: str, max_results: int = 25) -> list[InboundEmail]: ...


class VoiceGateway(Protocol):
    def place_call(self, to: str, url: str, status_callback: str, time_limit: int) -> str: ...


class CalendarGateway(Protocol):
    def create_event(self, title: str, start: datetime, end: datetime, location: str | None, description: str | None) -> str: ...
    def busy_periods(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]: ...
