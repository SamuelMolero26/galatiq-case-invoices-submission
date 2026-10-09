"""Reference Rates: pinned USD conversion for classification only; payments never convert."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from invoice_pipeline.model import UsdEquivalent


@dataclass(frozen=True)
class ReferenceRate:
    rate: Decimal  # USD per one unit of the currency
    as_of: date


AS_OF = date(2026, 1, 2)  # pinned date of every rate below, USD included
REFERENCE_RATES: dict[str, ReferenceRate] = {
    "EUR": ReferenceRate(rate=Decimal("1.08"), as_of=AS_OF)
}
# The buffer inflates the converted amount, so a rate that moved against us can only make the
# Heightened Scrutiny check (> $10,000) fire earlier, never later.
SAFETY_BUFFER = Decimal("0.05")
_USD = ReferenceRate(rate=Decimal(1), as_of=AS_OF)  # implicit, at par


def usd_equivalent(amount: Decimal, currency: str) -> UsdEquivalent | None:
    """The buffered USD value of `amount`; None when `currency` has no Reference Rate.

    USD is implicit: at par and never buffered. Nothing is rounded, so limit checks stay exact.
    """
    if currency == "USD":
        ref, buffer = _USD, Decimal(0)
    elif (ref := REFERENCE_RATES.get(currency)) is not None:
        buffer = SAFETY_BUFFER
    else:
        return None
    return UsdEquivalent(
        amount=amount * ref.rate * (1 + buffer), rate=ref.rate, as_of=ref.as_of, buffer=buffer
    )
