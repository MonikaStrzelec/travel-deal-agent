"""Pure notification decisions, independent of storage and delivery."""

from decimal import Decimal
from typing import Literal

AlertKind = Literal["new_offer", "price_drop"]


def classify_alert(price: Decimal, baseline: Decimal | None, returned: bool) -> AlertKind | None:
    """Alert once per eligible offer group, then only on a genuinely better price.

    `baseline` is the lowest price already alerted for the group; `None` means
    the group was never alerted. `returned` marks a group that had stopped
    qualifying long enough to be announced again as new. Any drop below the
    baseline alerts -- there is no minimum drop amount -- while an unchanged or
    higher price never repeats an alert.
    """
    if baseline is None or returned:
        return "new_offer"
    return "price_drop" if price < baseline else None
