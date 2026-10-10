"""Deterministic deal math. All figures come from inputs; nothing is looked up or guessed."""
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

DISCLAIMER = (
    "Estimates only, based solely on the inputs listed above. Not an appraisal, valuation, or "
    "guarantee of value or profit. Verify ARV, repair scope, and costs independently; this is "
    "not legal, tax, or lending advice."
)
CENT = Decimal("0.01")


class DealInputError(ValueError):
    pass


def _d(name: str, value, *, positive=False) -> Decimal:
    if value is None or isinstance(value, bool):
        raise DealInputError(f"{name} is required")
    try:
        d = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation as exc:
        raise DealInputError(f"{name} must be a number") from exc
    if not d.is_finite() or d < 0 or (positive and d == 0):
        raise DealInputError(f"{name} must be {'greater than' if positive else 'at least'} zero")
    return d


def _money(d: Decimal) -> str:
    return f"${d.quantize(CENT, ROUND_HALF_UP):,.2f}"


@dataclass
class DealResult:
    inputs: dict
    mao: Decimal
    viable: bool
    breakdown: list[dict]
    assumptions: list[str]
    warnings: list[str]
    buyer_check: dict | None = None
    sensitivity: list[dict] = field(default_factory=list)
    disclaimer: str = DISCLAIMER

    def to_dict(self) -> dict:
        return {
            "inputs": self.inputs, "mao": str(self.mao.quantize(CENT, ROUND_HALF_UP)), "viable": self.viable,
            "breakdown": self.breakdown, "assumptions": self.assumptions, "warnings": self.warnings,
            "buyer_check": self.buyer_check, "sensitivity": self.sensitivity, "disclaimer": self.disclaimer,
        }


def _mao(arv: Decimal, factor: Decimal, repairs: Decimal, closing: Decimal, fee: Decimal) -> Decimal:
    return arv * factor - repairs - closing - fee


def compute_mao(
    *, address: str, arv, repair_estimate, closing_costs, assignment_fee, discount_factor=Decimal("0.70"),
    arv_source: str = "user-provided estimate", buyer_requirements: dict | None = None,
) -> DealResult:
    if not address or not address.strip():
        raise DealInputError("address is required")
    arv_d = _d("arv", arv, positive=True)
    repairs = _d("repair_estimate", repair_estimate)
    closing = _d("closing_costs", closing_costs)
    fee = _d("assignment_fee", assignment_fee)
    factor = _d("discount_factor", discount_factor, positive=True)
    if factor > 1:
        raise DealInputError("discount_factor must be between 0 and 1 (e.g. 0.70)")

    mao = _mao(arv_d, factor, repairs, closing, fee)
    viable = mao > 0
    arv_part = (arv_d * factor)
    breakdown = [
        {"label": "ARV (as provided)", "amount": _money(arv_d)},
        {"label": f"x discount factor {factor}", "amount": _money(arv_part)},
        {"label": "- estimated repairs", "amount": _money(repairs)},
        {"label": "- closing costs", "amount": _money(closing)},
        {"label": "- desired assignment fee", "amount": _money(fee)},
        {"label": "= maximum allowable offer (MAO)", "amount": _money(mao)},
    ]
    assumptions = [
        "Formula: MAO = ARV x discount factor - repairs - closing costs - assignment fee",
        f"ARV {_money(arv_d)} is a {arv_source}; it was not verified or derived by this system.",
        f"Repair estimate {_money(repairs)} is user-provided; no inspection or contractor bid is implied.",
        f"Closing costs {_money(closing)} are user-provided.",
        f"Assignment fee {_money(fee)} is a target, not a guaranteed amount.",
        f"Discount factor {factor} is a business-rule setting, not a market fact.",
    ]
    warnings = []
    if not viable:
        warnings.append("MAO is zero or negative with these inputs: no offer works under this formula.")
    if factor > Decimal("0.85"):
        warnings.append("Discount factor above 0.85 leaves very little margin for the buyer.")
    if repairs > arv_d * Decimal("0.5"):
        warnings.append("Repairs exceed 50% of ARV; double-check scope and ARV.")

    sens = []
    for label, arv_mult, rep_mult in (("ARV -10%", "0.9", "1"), ("ARV +10%", "1.1", "1"), ("Repairs +20%", "1", "1.2")):
        v = _mao(arv_d * Decimal(arv_mult), factor, repairs * Decimal(rep_mult), closing, fee)
        sens.append({"scenario": label, "mao": _money(v)})
    assumptions.append("Sensitivity rows are arithmetic on your inputs, not forecasts.")

    buyer_check = None
    if buyer_requirements:
        buyer_price = mao + fee
        checks, ok = [], True
        if (mp := buyer_requirements.get("max_price")) is not None:
            passed = buyer_price <= _d("buyer max_price", mp)
            ok &= passed
            checks.append({"requirement": f"Buyer price {_money(buyer_price)} <= max price {_money(_d('mp', mp))}", "met": passed})
        if (pct := buyer_requirements.get("max_pct_of_arv")) is not None:
            limit = _d("buyer max_pct_of_arv", pct)
            limit = limit / 100 if limit > 1 else limit
            actual = buyer_price / arv_d
            passed = actual <= limit
            ok &= passed
            checks.append({"requirement": f"Buyer price is {actual:.1%} of ARV; buyer limit {limit:.1%}", "met": passed})
        buyer_check = {"buyer_price": _money(buyer_price), "all_met": ok and viable, "checks": checks}
        assumptions.append("Buyer price = MAO + assignment fee (what an end buyer would pay).")

    return DealResult(
        inputs={"address": address.strip(), "arv": str(arv_d), "repair_estimate": str(repairs),
                "closing_costs": str(closing), "assignment_fee": str(fee), "discount_factor": str(factor),
                "arv_source": arv_source, "buyer_requirements": buyer_requirements or {}},
        mao=mao, viable=viable, breakdown=breakdown, assumptions=assumptions, warnings=warnings,
        buyer_check=buyer_check, sensitivity=sens,
    )
