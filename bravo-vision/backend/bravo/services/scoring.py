"""Rule-based motivation scoring (0-100).

Deterministic on purpose: every point is traceable to a stated signal, so the
score can be audited and explained to the owner. The LLM extracts signals into
`discovery`; it never picks the number.
"""
import re
from dataclasses import dataclass

from bravo.config import get_settings


@dataclass
class ScoreResult:
    score: int
    reasons: list[dict]


_DISTRESS = [
    (r"foreclos|pre-?foreclos|auction|behind on (?:payments|mortgage)|notice of default", 25, "facing foreclosure / behind on payments"),
    (r"divorce|separat", 18, "divorce / separation"),
    (r"probate|inherit|estate|deceased|passed away", 18, "inherited / probate"),
    (r"tax lien|back taxes|delinquent tax|code violation|condemn", 18, "liens / code issues"),
    (r"relocat|job transfer|moving (?:out|away)|military|pcs", 14, "relocation"),
    (r"lost (?:my )?job|laid off|medical|hospital|health|bankrupt", 14, "financial or health hardship"),
    (r"tired landlord|bad tenant|evict|vacant|abandon", 12, "landlord / vacancy pain"),
    (r"need(?:s)? (?:to )?(?:sell|move) (?:fast|quick|asap)|asap|quick(?:ly)? sale", 10, "wants speed"),
]
_CONDITION = re.compile(r"needs? (?:work|repairs?|rehab)|poor|fire|flood|mold|roof|foundation|gutted|hoarder|as-?is|outdated|distressed", re.I)
_NEGATIVE = [
    (r"not interested|\bstop (?:texting|calling|contacting|messaging|emailing)|remove me|do not (?:call|text|contact)|wrong number|unsubscribe", -100, "seller declined contact"),
    (r"listed with|have an agent|realtor|on the mls", -25, "already listed with an agent"),
    (r"no rush|just curious|just looking|testing the market", -15, "no urgency / shopping"),
]
_FIRM_PRICE = re.compile(r"firm|full (?:retail|market)|zillow|retail|not (?:taking|accepting) less|won'?t go (?:below|under)", re.I)
_FLEX_PRICE = re.compile(r"flexible|open to|negotiable|make me an offer|whatever|depends", re.I)
_INBOUND_SOURCES = {"web_form", "email", "sms_reply", "inbound_call"}


def _timeline_days(text: str) -> int | None:
    t = text.lower()
    if re.search(r"asap|immediately|right away|this week|few days", t):
        return 7
    m = re.search(r"(\d+)\s*(day|week|month)", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return n * {"day": 1, "week": 7, "month": 30}[unit]
    if "month" in t:
        return 30
    if re.search(r"no rush|flexible|someday|eventually|not sure", t):
        return 365
    return None


def score_motivation(source: str, discovery: dict | None, text: str = "") -> ScoreResult:
    discovery = discovery or {}
    blob = " ".join([text or ""] + [str(v) for v in discovery.values() if v])
    points: list[tuple[int, str]] = []

    if source in _INBOUND_SOURCES:
        points.append((10, "seller reached out (inbound)"))

    days = _timeline_days(str(discovery.get("timeline", "")) + " " + (text or ""))
    if days is not None:
        if days <= 30:
            points.append((25, "wants to sell within 30 days"))
        elif days <= 90:
            points.append((15, "wants to sell within 90 days"))
        elif days <= 180:
            points.append((8, "wants to sell within 6 months"))

    best = max(((p, label) for pat, p, label in _DISTRESS if re.search(pat, blob, re.I)), default=None)
    if best:
        points.append(best)

    if _CONDITION.search(str(discovery.get("condition", "")) + " " + (text or "")):
        points.append((10, "property needs work"))
    occ = str(discovery.get("occupancy", "")).lower()
    if "vacant" in occ:
        points.append((8, "vacant property"))

    price = str(discovery.get("price_expectation", ""))
    if _FIRM_PRICE.search(price):
        points.append((-10, "price expectation looks firm"))
    elif _FLEX_PRICE.search(price):
        points.append((8, "flexible on price"))

    mortgage = str(discovery.get("mortgage", "")).lower()
    if re.search(r"free and clear|no mortgage|paid off|owned outright", mortgage):
        points.append((5, "no mortgage (flexible terms)"))

    for pat, p, label in _NEGATIVE:
        if re.search(pat, blob, re.I):
            points.append((p, label))

    answered = sum(1 for k in ("timeline", "condition", "mortgage", "occupancy", "price_expectation", "reason") if discovery.get(k))
    if answered >= 4:
        points.append((5, "engaged: answered most discovery questions"))

    total = max(0, min(100, sum(p for p, _ in points)))
    return ScoreResult(total, [{"signal": label, "points": p} for p, label in points])


def next_action_for(score: int, status: str, source: str) -> tuple[str, int, str]:
    """Return (action, priority 1-4, due_hint)."""
    hot = get_settings().hot_lead_threshold
    if status in ("do_not_contact", "dead", "duplicate"):
        return "No action", 4, "none"
    if score >= hot:
        return "Hot lead: call seller now and review draft reply", 1, "immediate"
    if score >= 50:
        return "Draft intro SMS for approval and call today", 2, "same_day"
    if score >= 30:
        return "Draft intro message for approval; add to nurture follow-up", 3, "next_day"
    return "Low priority: monthly check-in message after approval", 4, "week"
