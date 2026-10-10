from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bravo.services import compliance, deal_math, hours, normalize, scoring


def test_phone_normalization():
    assert normalize.normalize_phone("(512) 867-5309") == "+15128675309"
    assert normalize.normalize_phone("123") is None
    assert normalize.normalize_phone("512-867-5309") == "+15128675309"
    assert normalize.normalize_phone("+1 512 867 5309") == "+15128675309"
    assert normalize.normalize_phone("abc") is None and normalize.normalize_phone("") is None
    assert normalize.normalize_phone(None) is None


def test_email_normalization():
    assert normalize.normalize_email("  Jane@Example.COM ") == "jane@example.com"
    assert normalize.normalize_email("bad") is None


def test_address_normalization_variants_match():
    a = normalize.normalize_address(one_line="123 North Main Street, Apt 4B, Austin, Texas 78701-1234")
    b = normalize.normalize_address(street="123 N. Main St #4B", city="austin", state="TX", zip="78701")
    assert a["address_key"] == b["address_key"]
    assert a["state"] == "TX" and a["zip"] == "78701" and a["unit"] == "4B"
    assert normalize.normalize_address(street="") is None
    c = normalize.normalize_address(one_line="9 Oak Lane, Dallas, TX")
    assert c["city"] == "Dallas" and c["zip"] is None and "DALLAS|TX" in c["address_key"]
    assert normalize.normalize_address(one_line="9 Oak Lane")["state"] is None


def test_scoring_is_traceable_and_bounded():
    hot = scoring.score_motivation(
        "web_form",
        {"timeline": "need to sell in 3 weeks", "reason": "facing foreclosure", "condition": "needs roof repairs",
         "price_expectation": "open to offers", "occupancy": "vacant", "mortgage": "behind"},
    )
    assert hot.score >= 70 and all("signal" in r for r in hot.reasons)
    cold = scoring.score_motivation("list", {"timeline": "no rush", "price_expectation": "firm, zillow price"})
    assert cold.score < 20
    assert scoring.score_motivation("sms_reply", {}, "please stop texting me").score == 0
    assert scoring.score_motivation("list", {}, "").score == 0


def test_next_action_priorities():
    assert scoring.next_action_for(85, "new", "web_form")[1] == 1
    assert scoring.next_action_for(55, "new", "list")[1] == 2
    assert scoring.next_action_for(35, "new", "list")[1] == 3
    assert scoring.next_action_for(5, "new", "list")[1] == 4
    assert scoring.next_action_for(90, "do_not_contact", "list")[0] == "No action"


def test_contact_hours_recipient_local():
    # Tue 2026-03-03 15:00 UTC = 09:00 Chicago (CST) / 10:00 New York
    t = datetime(2026, 3, 3, 15, 0, tzinfo=UTC)
    assert hours.is_contact_allowed(t, "TX") and hours.is_contact_allowed(t, "NY")
    assert not hours.is_contact_allowed(datetime(2026, 3, 3, 14, 0, tzinfo=UTC), "TX")  # 8am Chicago
    assert not hours.is_contact_allowed(datetime(2026, 3, 8, 18, 0, tzinfo=UTC), "TX")  # Sunday
    assert not hours.is_contact_allowed(datetime(2026, 3, 4, 2, 0, tzinfo=UTC), "TX")  # Tue 8pm Chicago


def test_next_contact_time_rolls_forward():
    sat_night = datetime(2026, 3, 8, 3, 0, tzinfo=UTC)  # Sat 9pm Chicago
    nxt = hours.next_contact_time(sat_night, "TX")
    assert nxt.astimezone(hours.contact_tz("TX")).weekday() == 0 and hours.is_contact_allowed(nxt, "TX")
    inside = datetime(2026, 3, 3, 18, 0, tzinfo=UTC)
    assert hours.next_contact_time(inside, "TX") == inside
    early = datetime(2026, 3, 3, 12, 0, tzinfo=UTC)  # 6am Chicago
    assert hours.next_contact_time(early, "TX").astimezone(hours.contact_tz("TX")).hour == 9


def test_compliance_checks():
    assert compliance.check_message("Hi Jane, are you looking to sell your home on Oak St?", "sms") == []
    assert "no guarantees" in " ".join(compliance.check_message("We guarantee the best price", "sms"))
    assert compliance.check_message("This will lower your capital gains tax", "email")
    assert compliance.check_message("We can pay $150,000", "sms")
    assert compliance.check_message("We can pay $150,000", "sms", allow_amounts=True) == []
    assert compliance.check_message("Please consult a qualified tax professional about that.", "email") == []
    assert compliance.check_message("x" * 500, "sms") and compliance.check_message("  ", "sms")
    assert compliance.check_message("Act now!", "sms")
    assert compliance.check_message("We do not guarantee any outcome, and this is not an appraisal.", "sms") == []
    assert compliance.check_message("Not only do we guarantee it, it is risk-free", "sms")


def test_keywords_and_footer():
    assert compliance.classify_keyword(" STOP ") == "stop" and compliance.classify_keyword("Unsubscribe") == "stop"
    assert compliance.classify_keyword("start") == "start" and compliance.classify_keyword("help") == "help"
    assert compliance.classify_keyword("stop calling me about the roof") is None
    out = compliance.with_footer("Hello", "sms")
    assert "Reply STOP" in out and compliance.with_footer(out, "sms") == out
    assert "Reply STOP" in compliance.with_footer("Hello", "email")


def test_mao_formula_and_assumptions():
    r = deal_math.compute_mao(address="1 A St", arv="200,000", repair_estimate=30000, closing_costs=4000,
                              assignment_fee=10000, discount_factor="0.70")
    assert r.mao == Decimal("96000.00") and r.viable
    d = r.to_dict()
    assert d["mao"] == "96000.00" and "not an appraisal" in d["disclaimer"].lower() or "Not an appraisal" in d["disclaimer"]
    assert any("not verified" in a for a in d["assumptions"]) and len(d["breakdown"]) == 6
    assert {s["scenario"] for s in d["sensitivity"]} == {"ARV -10%", "ARV +10%", "Repairs +20%"}


def test_mao_nonviable_and_buyer_check():
    r = deal_math.compute_mao(address="x", arv=100000, repair_estimate=60000, closing_costs=3000, assignment_fee=10000)
    assert not r.viable and len(r.warnings) >= 2
    ok = deal_math.compute_mao(address="x", arv=200000, repair_estimate=30000, closing_costs=4000, assignment_fee=10000,
                               buyer_requirements={"max_price": 110000, "max_pct_of_arv": 55})
    assert ok.buyer_check["buyer_price"] == "$106,000.00" and ok.buyer_check["all_met"]
    no = deal_math.compute_mao(address="x", arv=200000, repair_estimate=30000, closing_costs=4000, assignment_fee=10000,
                               buyer_requirements={"max_price": 100000})
    assert not no.buyer_check["all_met"]


@pytest.mark.parametrize("kw", [
    {"arv": 0}, {"arv": -5}, {"arv": "abc"}, {"arv": None}, {"repair_estimate": -1}, {"discount_factor": 1.5},
    {"discount_factor": 0}, {"address": " "}, {"arv": True},
])
def test_mao_rejects_bad_input(kw):
    base = dict(address="x", arv=100000, repair_estimate=1, closing_costs=1, assignment_fee=1)
    base.update(kw)
    with pytest.raises(deal_math.DealInputError):
        deal_math.compute_mao(**base)
