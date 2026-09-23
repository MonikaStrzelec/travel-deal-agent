"""Translate shared policy to the finite set of observed Rainbow UI options."""

from dataclasses import dataclass
from decimal import Decimal

from ..config_types import FilterConfig, ProviderConfig

AIRPORT_LABELS = {
    "LCJ": "Łódź",
    "WAW": "Warszawa Chopin",
    "WMI": "Warszawa Modlin",
    "KTW": "Katowice",
    "WRO": "Wrocław",
}
STAR_CODES = {3: "6", 4: "8", 5: "10"}
STAR_LABELS = {
    3: "Ocena: 3 gwiazdki na 5 ***",
    4: "Ocena: 4 gwiazdki na 5 ****",
    5: "Ocena: 5 gwiazdek na 5 *****",
}
MEALS = {
    "AI": ("All inclusive", "all-inclusive"),
    "FB": ("3 posiłki", "3-posilki"),
    "HB": ("2 posiłki", "2-posilki"),
    "BB": ("Śniadania", "sniadania"),
}
DURATIONS = {(7, 9): "7 - 9 dni", (10, 13): "10 - 13 dni", (14, 17): "14 - 17 dni"}


def star_options(minimum: float) -> tuple[int, ...]:
    if minimum not in STAR_CODES:
        raise ValueError("Rainbow supports minimum stars 3, 4 or 5")
    return tuple(star for star in STAR_CODES if star >= minimum)


@dataclass(frozen=True)
class SearchPlan:
    airports: tuple[str, ...]
    stars: tuple[int, ...]
    boards: tuple[str, ...]
    min_days: int
    max_days: int
    max_price: Decimal
    rating_floor: int | None

    @classmethod
    def from_filters(cls, filters: FilterConfig) -> "SearchPlan":
        if filters["people"] != 2 or filters["currency"] != "PLN":
            raise ValueError("Rainbow currently supports two adults / one room / PLN only")
        if not filters["airports"] or not set(filters["airports"]) <= AIRPORT_LABELS.keys():
            raise ValueError("Unsupported Rainbow airport selection")
        maximum = filters["max_days"]
        if maximum is None or (filters["min_days"], maximum) not in DURATIONS:
            raise ValueError("Rainbow duration must use an observed preset: 7-9, 10-13 or 14-17")
        if not filters["allowed_boards"] or not set(filters["allowed_boards"]) <= MEALS.keys():
            raise ValueError("Unsupported Rainbow meal option")
        rule = filters["provider_ratings"].get("rainbow")
        budget = Decimal(filters["max_price"])
        if not budget.is_finite() or budget <= 0:
            raise ValueError("Rainbow requires a finite positive price cap")
        floor = None
        if rule and rule["enabled"]:
            if rule["scale"] != {"min": 0, "max": 6} or not rule["price_bands"]:
                raise ValueError("Rainbow requires its native 0-6 rating scale and price bands")
            minimum = min(band["min_rating"] for band in rule["price_bands"])
            # Select a conservative UI floor; shared rules recheck exact price-band thresholds.
            floor = max((n for n in (3, 4, 5) if n <= minimum), default=None)
        return cls(
            tuple(filters["airports"]),
            star_options(filters["min_stars"]),
            tuple(filters["allowed_boards"]),
            filters["min_days"],
            maximum,
            budget,
            floor,
        )


@dataclass(frozen=True)
class Limits:
    max_offers: int = 10
    max_analyzed_cards: int = 15
    max_scrolls: int = 0
    timeout_seconds: int = 20
    cycle_seconds: int = 180
    debug: bool = False
    max_detail_requests: int = 1

    @classmethod
    def from_config(cls, config: ProviderConfig) -> "Limits":
        result = cls(
            config.get("max_offers", 10),
            config.get("max_analyzed_cards", 15),
            config.get("max_scrolls", 0),
            config.get("timeout_seconds", 20),
            config.get("cycle_seconds", 180),
            config.get("debug", False),
            config.get("max_detail_requests", 1),
        )
        if (
            min(
                result.max_offers,
                result.max_analyzed_cards,
                result.timeout_seconds,
                result.cycle_seconds,
            )
            < 1
            or result.max_scrolls < 0
            or result.max_detail_requests < 0
        ):
            raise ValueError("Rainbow limits must be positive; scroll/detail budgets may be zero")
        if result.max_offers > result.max_analyzed_cards:
            raise ValueError("Rainbow max_offers cannot exceed max_analyzed_cards")
        return result
