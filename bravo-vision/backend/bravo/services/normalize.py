import re

import phonenumbers

STATES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA", "COLORADO": "CO",
    "CONNECTICUT": "CT", "DELAWARE": "DE", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
    "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA",
    "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX",
    "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY", "DISTRICT OF COLUMBIA": "DC",
}
STATE_CODES = set(STATES.values())

SUFFIXES = {
    "STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "DRIVE": "DR", "LANE": "LN", "COURT": "CT",
    "BOULEVARD": "BLVD", "CIRCLE": "CIR", "PLACE": "PL", "TERRACE": "TER", "HIGHWAY": "HWY",
    "PARKWAY": "PKWY", "TRAIL": "TRL", "WAY": "WAY", "SQUARE": "SQ",
}
DIRECTIONS = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
              "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW"}
UNIT_RE = re.compile(r"(?:\b(?:APT|APARTMENT|UNIT|STE|SUITE)|#)\s*\.?\s*([A-Z0-9-]+)\s*$")
UNIT_PART_RE = re.compile(r"^(?:apt|apartment|unit|ste|suite|#)\s*\.?\s*[\w-]+$", re.I)


def normalize_phone(raw: str | None, region: str = "US") -> str | None:
    """Return E.164 or None when the number is not valid."""
    if not raw or not str(raw).strip():
        return None
    try:
        parsed = phonenumbers.parse(str(raw), region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    email = raw.strip().lower()
    return email if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) else None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[.,]", " ", text.upper())).strip()


def _normalize_state(token: str | None) -> str | None:
    if not token:
        return None
    t = _clean(token)
    if t in STATE_CODES:
        return t
    return STATES.get(t)


def parse_one_line(address: str) -> dict:
    """Best-effort split of '123 Main St, Austin, TX 78701'. Missing parts stay None."""
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if len(parts) > 1 and UNIT_PART_RE.match(parts[1]):
        parts = [f"{parts[0]} {parts[1]}"] + parts[2:]
    out = {"street": parts[0] if parts else "", "city": None, "state": None, "zip": None}
    if len(parts) >= 2:
        tail = parts[-1]
        m = re.fullmatch(r"(.*?)\s*(\d{5})(?:-\d{4})?", tail)
        zip_ = None
        if m:
            tail, zip_ = m.group(1).strip(), m.group(2)
        state = _normalize_state(tail) if tail else None
        if state or zip_:
            out["state"], out["zip"] = state, zip_
            if not state and tail:
                m2 = re.fullmatch(r"(.*)\s+([A-Za-z]{2})", tail)
                if m2 and m2.group(2).upper() in STATE_CODES:
                    out["state"] = m2.group(2).upper()
                    out["city"] = m2.group(1)
            mid = parts[1:-1]
            if mid and not out["city"]:
                out["city"] = mid[-1]
        else:
            out["city"] = parts[1]
    return out


def normalize_address(
    street: str | None = None, city: str | None = None, state: str | None = None,
    zip: str | None = None, one_line: str | None = None,
) -> dict | None:
    if one_line and not street:
        parsed = parse_one_line(one_line)
        street, city, state, zip = parsed["street"], city or parsed["city"], state or parsed["state"], zip or parsed["zip"]
    if not street or not street.strip():
        return None
    s = _clean(street.replace('#', ' # '))
    unit = None
    m = UNIT_RE.search(s)
    if m:
        unit = m.group(1)
        s = s[: m.start()].strip()
    tokens = [DIRECTIONS.get(t, SUFFIXES.get(t, t)) for t in s.split()]
    street_norm = " ".join(tokens)
    zip5 = None
    if zip:
        zm = re.match(r"\d{5}", str(zip).strip())
        zip5 = zm.group(0) if zm else None
    state_norm = _normalize_state(state)
    city_norm = _clean(city) if city else None
    locality = zip5 or f"{city_norm or ''}|{state_norm or ''}"
    return {
        "street": street_norm,
        "unit": unit,
        "city": city.strip().title() if city else None,
        "state": state_norm,
        "zip": zip5,
        "address_key": f"{street_norm}|{unit or ''}|{locality}",
    }
