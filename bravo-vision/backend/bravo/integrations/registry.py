"""Lazily-built gateways. Tests (and alternative deployments) swap them with `configure`."""
from dataclasses import dataclass

from bravo.config import get_settings


@dataclass
class Gateways:
    sms: object | None = None
    email: object | None = None
    calendar: object | None = None
    voice: object | None = None
    crm: object | None = None


_g = Gateways()


def configure(**kwargs) -> None:
    for k, v in kwargs.items():
        setattr(_g, k, v)


def reset() -> None:
    configure(sms=None, email=None, calendar=None, voice=None, crm=None)


def sms():
    if _g.sms is None:
        from bravo.integrations.twilio_sms import TwilioSMS

        _g.sms = TwilioSMS()
    return _g.sms


def voice():
    if _g.voice is None:
        from bravo.integrations.twilio_sms import TwilioVoice

        _g.voice = TwilioVoice()
    return _g.voice


def email():
    if _g.email is None:
        from bravo.integrations.google_api import GmailGateway

        _g.email = GmailGateway()
    return _g.email


def calendar():
    if _g.calendar is None:
        from bravo.integrations.google_api import GoogleCalendar

        _g.calendar = GoogleCalendar()
    return _g.calendar


def crm():
    if _g.crm is None:
        provider = get_settings().crm_provider.lower()
        if provider == "hubspot":
            from bravo.integrations.crm.hubspot import HubSpotCRM

            _g.crm = HubSpotCRM()
        elif provider == "bravo":
            from bravo.integrations.crm.bravo import BravoVisionCRM

            _g.crm = BravoVisionCRM()
        elif provider == "local":
            from bravo.integrations.crm.local import LocalCRM

            _g.crm = LocalCRM()
        else:
            raise ValueError(f"unknown CRM provider '{provider}'")
    return _g.crm
