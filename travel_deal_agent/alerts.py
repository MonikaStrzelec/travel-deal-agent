"""Pure notification decisions, independent of storage and delivery."""

from decimal import Decimal
from typing import Literal

AlertKind = Literal["new_offer", "price_drop"]


def classify_alert(
    price: Decimal, baseline: Decimal | None, threshold: Decimal
) -> AlertKind | None:
    """Alert on first eligibility or a cumulative drop below the last alert baseline."""
    if not threshold.is_finite() or threshold <= 0:
        raise ValueError("Alert threshold must be finite and positive")
    if baseline is None:
        return "new_offer"
    return "price_drop" if baseline - price >= threshold else None
