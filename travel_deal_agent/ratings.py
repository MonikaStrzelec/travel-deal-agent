"""Configurable native rating scales and explicit price-band boundaries."""

import math
from decimal import Decimal

from .config_types import RatingRule, RatingScale
from .models import Offer


def normalize_rating(value: float | None, scale: RatingScale | None) -> float | None:
    """Map the configured minimum to 0 and maximum to 100; unknown stays None."""
    if value is None or scale is None:
        return None
    low, high = scale["min"], scale["max"]
    if not math.isfinite(value) or not low <= value <= high:
        return None
    return 100 * (value - low) / (high - low)


def rating_matches(offer: Offer, rules: dict[str, RatingRule]) -> bool:
    rule = rules.get(offer.provider)
    if rule is None:
        return False
    if not rule["enabled"]:
        return True
    if (
        offer.rating is None
        or normalize_rating(offer.rating, rule["scale"]) is None
        or offer.price_per_person is None
    ):
        return False
    for band in rule["price_bands"]:
        maximum = Decimal(str(band["max_price"]))
        below_maximum = offer.price_per_person < maximum or (
            band.get("max_inclusive", False) and offer.price_per_person == maximum
        )
        if Decimal(str(band["min_price"])) <= offer.price_per_person and below_maximum:
            return offer.rating >= band["min_rating"]
    return False


def validate_rating_rules(rules: dict[str, RatingRule]) -> None:
    for name, rule in rules.items():
        if type(rule["enabled"]) is not bool:
            raise ValueError(f"Invalid rating enabled flag for {name}")
        scale = rule["scale"]
        if scale is not None and (
            not math.isfinite(scale["min"])
            or not math.isfinite(scale["max"])
            or scale["min"] >= scale["max"]
        ):
            raise ValueError(f"Invalid rating scale for {name}")
        if rule["enabled"] and (scale is None or not rule["price_bands"]):
            raise ValueError(f"Enabled rating rules require a scale and bands: {name}")
        previous_end = None
        previous_inclusive = False
        for band in rule["price_bands"]:
            low, high = (Decimal(str(band[k])) for k in ("min_price", "max_price"))
            if not low.is_finite() or not high.is_finite() or not 0 <= low < high:
                raise ValueError(f"Invalid price band for {name}")
            if type(band.get("max_inclusive", False)) is not bool:
                raise ValueError(f"Invalid max_inclusive flag for {name}")
            if previous_end is not None and (
                low < previous_end or (low == previous_end and previous_inclusive)
            ):
                raise ValueError(f"Rating price bands must be ordered and non-overlapping: {name}")
            if normalize_rating(band["min_rating"], scale) is None:
                raise ValueError(f"Invalid minimum rating for {name}")
            previous_end = high
            previous_inclusive = band.get("max_inclusive", False)
