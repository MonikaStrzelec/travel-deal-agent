"""Pure notification decisions, independent of storage and delivery.

Price history vs. attractiveness: this module only decides whether something
*happened* to an offer's price (new/returned/dropped/new low) -- never
whether the offer is itself good (HOT/GOOD/MATCH). Attractiveness is computed
separately, fresh, at render time (`attractiveness.classify_offer`), and
never feeds back into this classification.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

AlertKind = Literal["new_offer", "returned", "price_drop", "new_low"]


@dataclass(frozen=True)
class PriceDropThreshold:
    """A drop is significant once it clears an absolute PLN amount OR a
    percentage of the reference price -- whichever is easier to reach.

    The OR combination catches both a large drop on an expensive offer (small
    percentage, but real money) and a large percentage drop on a cheap offer
    (small PLN amount, but proportionally big). `min_amount=0, min_percent=0`
    (the default used where no configuration is injected, e.g. in tests that
    do not care about noise control) makes every drop significant, matching
    this project's original threshold-free behavior.
    """

    min_amount: Decimal
    min_percent: Decimal

    def met(self, drop: Decimal, reference: Decimal) -> bool:
        return drop >= self.min_amount or drop >= reference * self.min_percent


# Threshold-free default: every drop is significant. Used where no
# configuration is injected (e.g. `Store`'s default), matching this
# project's original threshold-free behavior.
NO_MINIMUM_DROP = PriceDropThreshold(Decimal(0), Decimal(0))


def classify_alert(
    price: Decimal,
    baseline: Decimal | None,
    returned: bool,
    previous_price: Decimal | None,
    lowest_price: Decimal | None,
    threshold: PriceDropThreshold,
) -> AlertKind | None:
    """Classify one eligible observation into at most one price event.

    `baseline` is only ever used as an existence marker here (was this offer
    group ever alerted before?) -- `None` means never alerted, so this is
    unconditionally `new_offer`. `returned` marks a group that stopped
    qualifying for at least the configured re-arm window and is now eligible
    again.

    `previous_price` and `lowest_price` describe this exact offer's own prior
    observations (see `storage.Store.observe`): the price immediately before
    this one, and the lowest price ever recorded for it, respectively. Both
    are `None` when there is no prior history to compare against (e.g. a
    genuinely first observation), in which case neither `new_low` nor
    `price_drop` can ever fire -- only `new_offer`/`returned` can.

    Priority when more than one condition would technically apply -- most
    notably a drop that is *also* a new historical low -- is:
    new_low > price_drop > returned > new_offer. A single scan therefore
    never queues two notifications for the same offer.
    """
    if (
        lowest_price is not None
        and price < lowest_price
        and threshold.met(lowest_price - price, lowest_price)
    ):
        return "new_low"
    if (
        previous_price is not None
        and price < previous_price
        and threshold.met(previous_price - price, previous_price)
    ):
        return "price_drop"
    if baseline is None:
        return "new_offer"
    if returned:
        return "returned"
    return None
