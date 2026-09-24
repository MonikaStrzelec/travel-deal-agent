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
    min_days: int | None
    max_days: int | None
    max_price: Decimal
    rating_floor: int | None

    @classmethod
    def from_filters(cls, filters: FilterConfig) -> "SearchPlan":
        if filters["people"] != 2 or filters["currency"] != "PLN":
            raise ValueError("Rainbow currently supports two adults / one room / PLN only")
        if not filters["airports"] or not set(filters["airports"]) <= AIRPORT_LABELS.keys():
            raise ValueError("Unsupported Rainbow airport selection")
        min_nights, max_nights = filters["min_nights"], filters["max_nights"]
        min_days_native: int | None
        max_days_native: int | None
        if min_nights is None and max_nights is None:
            # No stay-length restriction: leave Rainbow's own duration filter
            # untouched. Its default query state before any duration control is
            # touched is "dlugoscPobytu=*-*" (see rainbow_browser.py `prepare()`),
            # i.e. every length -- exactly the unrestricted behaviour requested.
            min_days_native = max_days_native = None
        elif min_nights is None or max_nights is None:
            raise ValueError("Rainbow requires both min_nights and max_nights, or neither")
        else:
            # Rainbow's own site counts stay length as "dni" (nights + 1); translate the
            # canonical nights-based filter into that native encoding before matching one
            # of Rainbow's three observed UI presets.
            min_days_native, max_days_native = min_nights + 1, max_nights + 1
            if (min_days_native, max_days_native) not in DURATIONS:
                raise ValueError(
                    "Rainbow duration must use an observed nights preset: 6-8, 9-12 or 13-16"
                )
        # Only the meal options Rainbow's own UI can select are used to narrow the
        # search; a shared board (e.g. "ZO") that Rainbow has no confirmed checkbox
        # for is simply not selected there -- `filtering.matches_criteria` remains
        # the acceptance authority and Rainbow's own board mapping (`boards.py`,
        # no "rainbow" entry in `_PROVIDER_CODES`) can never produce that board
        # anyway, so this narrows nothing a real Rainbow offer could satisfy.
        supported_boards = tuple(b for b in filters["allowed_boards"] if b in MEALS)
        if not supported_boards:
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
            supported_boards,
            min_days_native,
            max_days_native,
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
