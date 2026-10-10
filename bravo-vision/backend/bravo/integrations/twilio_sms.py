from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from bravo.config import get_settings
from bravo.integrations.base import IntegrationNotConfigured, PermanentSendError, SendResult
from bravo.retry import TransientError, with_retry


class TwilioSMS:
    def __init__(self, client=None, from_number: str | None = None, sleep=None):
        s = get_settings()
        self.from_number = from_number or s.twilio_from_number
        if client is None:
            if not (s.twilio_account_sid and s.twilio_auth_token and self.from_number):
                raise IntegrationNotConfigured("Twilio credentials / from number are not configured")
            client = Client(s.twilio_account_sid, s.twilio_auth_token)
        self.client = client
        self._retry_kwargs = {"sleep": sleep} if sleep else {}

    def send(self, to: str, body: str) -> SendResult:
        def _once() -> SendResult:
            try:
                msg = self.client.messages.create(to=to, from_=self.from_number, body=body)
            except TwilioRestException as exc:
                if exc.status == 429 or exc.status >= 500:
                    raise TransientError(str(exc)) from exc
                raise PermanentSendError(f"Twilio rejected message ({exc.status}): {exc.msg}") from exc
            return SendResult(external_id=msg.sid)

        return with_retry(_once, **self._retry_kwargs)


class TwilioVoice:
    def __init__(self, client=None, from_number: str | None = None, sleep=None):
        s = get_settings()
        self.from_number = from_number or s.twilio_from_number
        if client is None:
            if not (s.twilio_account_sid and s.twilio_auth_token and self.from_number):
                raise IntegrationNotConfigured("Twilio credentials / from number are not configured")
            client = Client(s.twilio_account_sid, s.twilio_auth_token)
        self.client = client
        self._retry_kwargs = {"sleep": sleep} if sleep else {}

    def place_call(self, to: str, url: str, status_callback: str, time_limit: int) -> str:
        def _once() -> str:
            try:
                call = self.client.calls.create(to=to, from_=self.from_number, url=url, method="POST", status_callback=status_callback,
                                                status_callback_method="POST", status_callback_event=["completed"], time_limit=time_limit)
            except TwilioRestException as exc:
                if exc.status == 429 or exc.status >= 500:
                    raise TransientError(str(exc)) from exc
                raise PermanentSendError(f"Twilio rejected call ({exc.status}): {exc.msg}") from exc
            return call.sid

        return with_retry(_once, **self._retry_kwargs)


def valid_twilio_signature(url: str, params: dict, signature: str) -> bool:
    token = get_settings().twilio_auth_token
    if not token or not signature:
        return False
    return RequestValidator(token).validate(url, params, signature)
