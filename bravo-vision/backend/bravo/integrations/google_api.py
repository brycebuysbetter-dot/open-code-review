import base64
from datetime import datetime
from email.message import EmailMessage

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials

from bravo.config import get_settings
from bravo.integrations.base import InboundEmail, IntegrationNotConfigured, PermanentSendError, SendResult
from bravo.retry import TransientError, with_retry

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def _credentials() -> Credentials:
    s = get_settings()
    if not (s.google_client_id and s.google_client_secret and s.google_refresh_token):
        raise IntegrationNotConfigured("Google OAuth credentials are not configured")
    return Credentials(
        None, refresh_token=s.google_refresh_token, client_id=s.google_client_id,
        client_secret=s.google_client_secret, token_uri="https://oauth2.googleapis.com/token", scopes=SCOPES,
    )


def _call(request, sleep=None):
    def _once():
        try:
            return request.execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", 500)
            if status == 429 or status >= 500:
                raise TransientError(str(exc)) from exc
            raise PermanentSendError(f"Google API error {status}") from exc

    return with_retry(_once, **({"sleep": sleep} if sleep else {}))


def _body_text(payload: dict) -> str:
    if payload.get("mimeType", "").startswith("text/plain") and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        text = _body_text(part)
        if text:
            return text
    return ""


class GmailGateway:
    def __init__(self, service=None, sender: str | None = None, sleep=None):
        self.service = service or build("gmail", "v1", credentials=_credentials(), cache_discovery=False)
        self.sender = sender or get_settings().business_email
        self._sleep = sleep

    def send(self, to: str, subject: str, body: str) -> SendResult:
        msg = EmailMessage()
        msg["To"], msg["Subject"] = to, subject
        if self.sender:
            msg["From"] = self.sender
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        res = _call(self.service.users().messages().send(userId="me", body={"raw": raw}), self._sleep)
        return SendResult(external_id=res["id"])

    def fetch_inbound(self, query: str, max_results: int = 25) -> list[InboundEmail]:
        listing = _call(self.service.users().messages().list(userId="me", q=query, maxResults=max_results), self._sleep)
        out = []
        for ref in listing.get("messages", []):
            msg = _call(self.service.users().messages().get(userId="me", id=ref["id"], format="full"), self._sleep)
            headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
            out.append(InboundEmail(ref["id"], headers.get("from", ""), headers.get("subject", ""), _body_text(msg["payload"])))
        return out


class GoogleCalendar:
    def __init__(self, service=None, calendar_id: str | None = None, sleep=None):
        self.service = service or build("calendar", "v3", credentials=_credentials(), cache_discovery=False)
        self.calendar_id = calendar_id or get_settings().google_calendar_id
        self._sleep = sleep

    def create_event(self, title, start: datetime, end: datetime, location=None, description=None) -> str:
        # No attendees: inviting the seller would email them, which is an outbound message and needs approval.
        body = {"summary": title, "location": location, "description": description,
                "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}
        res = _call(self.service.events().insert(calendarId=self.calendar_id, body=body), self._sleep)
        return res["id"]

    def busy_periods(self, start: datetime, end: datetime):
        body = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": self.calendar_id}]}
        res = _call(self.service.freebusy().query(body=body), self._sleep)
        busy = res.get("calendars", {}).get(self.calendar_id, {}).get("busy", [])
        return [(datetime.fromisoformat(b["start"].replace("Z", "+00:00")), datetime.fromisoformat(b["end"].replace("Z", "+00:00"))) for b in busy]
