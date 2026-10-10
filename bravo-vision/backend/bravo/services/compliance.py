"""Guardrails applied to every outbound draft before a human even sees it."""
import re

from bravo.config import get_settings

STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout", "opt-out", "revoke"}
START_WORDS = {"start", "unstop", "yes"}
HELP_WORDS = {"help", "info"}

_GUARANTEE = re.compile(r"\b(guarantee[sd]?|risk[- ]free|no[- ]risk|can'?t lose|promise)\b", re.I)
_LEGAL_TAX_LENDING = re.compile(
    r"\b(tax(?:es)?|capital gains?|1031|deduct\w*|legal advice|your legal rights|sue|lawsuit|"
    r"loan modification|refinanc\w*|foreclosure (?:defense|rescue)|bankruptcy (?:advice|filing)|"
    r"you(?:'| a)?re entitled)\b",
    re.I,
)
_SAFE_DEFLECTION = re.compile(r"[^.!?]*\bconsult (?:a |an |your )?(?:qualified )?(?:attorney|lawyer|tax (?:professional|advisor)|financial advisor|lender)[^.!?]*[.!?]?", re.I)
_NEGATED_GUARANTEE = re.compile(r"\b(?:not|no|never|without|n't)\b(?!\s+only)[^.!?]{0,40}?\bguarantee\w*", re.I)
_MONEY = re.compile(r"\$\s?\d|\b\d[\d,]*\s?(?:dollars|usd|k)\b", re.I)
_PRESSURE = re.compile(r"\b(act now|last chance|offer expires|only today)\b", re.I)


def classify_keyword(body: str) -> str | None:
    word = re.sub(r"[^\w-]", "", body.strip().lower())
    if word in STOP_WORDS:
        return "stop"
    if word in START_WORDS:
        return "start"
    if word in HELP_WORDS:
        return "help"
    return None


def footer(channel: str) -> str:
    s = get_settings()
    if channel == "sms":
        return f" - {s.business_name} (automated assistant). Reply STOP to opt out."
    addr = f"\n{s.business_address}" if s.business_address else ""
    return f"\n\n{s.business_name} (automated assistant){addr}\nReply STOP to stop receiving messages."


def with_footer(body: str, channel: str) -> str:
    body = body.rstrip()
    return body if "reply stop" in body.lower() else body + footer(channel)


def check_message(body: str, channel: str, *, allow_amounts: bool = False) -> list[str]:
    """Return a list of violations (empty = compliant). Footer is added separately."""
    problems: list[str] = []
    scan = _NEGATED_GUARANTEE.sub(" ", _SAFE_DEFLECTION.sub(" ", body))
    if _GUARANTEE.search(scan):
        problems.append("no guarantees or risk-free claims")
    if _LEGAL_TAX_LENDING.search(scan):
        problems.append("no legal, tax, or lending advice; suggest consulting a qualified professional instead")
    if not allow_amounts and _MONEY.search(scan):
        problems.append("no dollar figures in conversation drafts; prices only go out via an approved offer")
    if _PRESSURE.search(scan):
        problems.append("no high-pressure language")
    if channel == "sms" and len(body) > 480:
        problems.append("SMS body too long (max 480 characters before footer)")
    if not body.strip():
        problems.append("empty message")
    return problems
