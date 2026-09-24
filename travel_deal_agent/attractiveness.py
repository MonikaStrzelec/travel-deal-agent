"""Presentation-only "is this offer good, right now" classification (V0).

Deliberately independent of `ranking.score`/`Offer.final_score`, which remains
the existing internal sort order and is never touched here. Classification is
always computed fresh from an `Offer` and the current config -- it is never
stored on the `Offer`, never written to SQLite, and never affects
`filtering.matches()`. An offer must already be eligible before this module is
asked to classify it.

Four independent areas, each reduced to a simple level (never a 0-100 score):
VALUE and HOTEL QUALITY can be strong/normal/weak; AIRPORT and BOARD can only
be strong/normal/neutral -- an allowed airport/board that is not in a "strong"
or "normal" tier is merely unremarkable, never penalized.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .config_types import AttractivenessConfig, RatingRule
from .models import Offer
from .ratings import normalize_rating

Level = Literal["strong", "normal", "weak", "neutral"]
Attractiveness = Literal["HOT", "GOOD", "MATCH"]

# V0 starting values, agreed with the project owner. Deliberately easy to
# retune from config.json once more real, cross-provider data (ITAKA, more
# boards, an LCJ departure, ...) exists -- see README "Attractiveness
# classification". Mirrors config.json's "attractiveness" section exactly;
# used whenever a caller does not explicitly pass its own config (e.g. a test
# that does not care about classification).
DEFAULT_ATTRACTIVENESS_CONFIG: AttractivenessConfig = {
    "value": {
        "strong_max_price_per_person_per_night": "170",
        "normal_max_price_per_person_per_night": "220",
    },
    "hotel_quality": {
        "strong_min_normalized_rating": 0.85,
        "normal_min_normalized_rating": 0.75,
    },
    "airport": {
        "strong": ["LCJ"],
        "normal": ["WAW", "WMI"],
    },
    "board": {
        "strong": ["AI", "UAI"],
        "normal": ["FB", "HB"],
        "neutral": ["ZO"],
    },
}


def _positive_decimal(value: str, label: str) -> Decimal:
    number = Decimal(value)
    if not number.is_finite() or number <= 0:
        raise ValueError(f"Attractiveness {label} must be finite and positive")
    return number


def validate_attractiveness_config(config: AttractivenessConfig) -> None:
    """Small, focused checks -- not a general-purpose config validation framework."""
    value = config["value"]
    strong_price = _positive_decimal(
        value["strong_max_price_per_person_per_night"], "strong VALUE threshold"
    )
    normal_price = _positive_decimal(
        value["normal_max_price_per_person_per_night"], "normal VALUE threshold"
    )
    if strong_price >= normal_price:
        raise ValueError("Attractiveness strong VALUE threshold must be below the normal one")

    hotel = config["hotel_quality"]
    strong_rating = hotel["strong_min_normalized_rating"]
    normal_rating = hotel["normal_min_normalized_rating"]
    for rating_value, label in ((strong_rating, "strong"), (normal_rating, "normal")):
        if not 0 <= rating_value <= 1:
            raise ValueError(f"Attractiveness {label} HOTEL QUALITY threshold must be in [0, 1]")
    if strong_rating < normal_rating:
        raise ValueError(
            "Attractiveness strong HOTEL QUALITY threshold must be at least the normal one"
        )

    airport = config["airport"]
    if set(airport["strong"]) & set(airport["normal"]):
        raise ValueError("Attractiveness AIRPORT tiers must not overlap")

    board = config["board"]
    board_tiers = (set(board["strong"]), set(board["normal"]), set(board["neutral"]))
    if any(a & b for i, a in enumerate(board_tiers) for b in board_tiers[i + 1 :]):
        raise ValueError("Attractiveness BOARD tiers must not overlap")


@dataclass(frozen=True)
class AttractivenessBreakdown:
    """One classification result; never persisted, always recomputed on demand."""

    value: Level
    hotel_quality: Level
    airport: Level
    board: Level
    category: Attractiveness


def classify_value(
    price_per_person: Decimal | None, nights: int | None, config: AttractivenessConfig
) -> Level:
    """The single VALUE signal: price_per_person_per_night. Never a second,
    separate price_per_person signal -- that would score cheapness twice."""
    if price_per_person is None or nights is None or nights <= 0:
        return "weak"
    thresholds = config["value"]
    per_night = price_per_person / nights
    if per_night <= Decimal(thresholds["strong_max_price_per_person_per_night"]):
        return "strong"
    if per_night <= Decimal(thresholds["normal_max_price_per_person_per_night"]):
        return "normal"
    return "weak"


def classify_hotel_quality(
    offer: Offer, provider_ratings: Mapping[str, RatingRule], config: AttractivenessConfig
) -> Level:
    """Provider rating (via the existing shared `normalize_rating`) is the base
    signal; stars only ever nudge that base level up by one step, never combined
    into a bigger score. Reviews are intentionally not used in V0: every real
    offer collected so far has `number_of_reviews=None`, so there is no data to
    calibrate a "large number of reviews" threshold against, and a guessed
    magic number would be worse than no rule. Absence must never count against
    the offer, so it is simply not scored -- neither None nor a low count ever
    lowers this level below what the rating/stars alone would already give.
    """
    rule = provider_ratings.get(offer.provider)
    normalized = normalize_rating(offer.rating, rule["scale"] if rule else None)
    thresholds = config["hotel_quality"]
    if normalized is None:
        base: Level = "weak"
    else:
        fraction = normalized / 100
        if fraction >= thresholds["strong_min_normalized_rating"]:
            base = "strong"
        elif fraction >= thresholds["normal_min_normalized_rating"]:
            base = "normal"
        else:
            base = "weak"
    stars = offer.hotel_stars
    if base == "normal" and stars is not None and stars >= 5:
        return "strong"
    if base == "weak" and stars is not None and stars >= 4:
        return "normal"
    return base


def classify_airport(departure_airport: str | None, config: AttractivenessConfig) -> Level:
    tiers = config["airport"]
    if departure_airport in tiers["strong"]:
        return "strong"
    if departure_airport in tiers["normal"]:
        return "normal"
    return "neutral"


def classify_board(board_type: str | None, config: AttractivenessConfig) -> Level:
    tiers = config["board"]
    if board_type in tiers["strong"]:
        return "strong"
    if board_type in tiers["normal"]:
        return "normal"
    return "neutral"


def _final_category(levels: tuple[Level, Level, Level, Level]) -> Attractiveness:
    """AIRPORT/BOARD never produce "weak", so they can only ever help toward
    HOT/GOOD, never count against an offer here. A single strong area (e.g.
    LCJ alone) is never enough for HOT -- it requires two areas agreeing."""
    strong_count = sum(1 for level in levels if level == "strong")
    weak_count = sum(1 for level in levels if level == "weak")
    if strong_count >= 2 and weak_count == 0:
        return "HOT"
    if strong_count >= 1 and weak_count <= 1:
        return "GOOD"
    return "MATCH"


def classify_offer(
    offer: Offer,
    provider_ratings: Mapping[str, RatingRule],
    config: AttractivenessConfig | None = None,
) -> AttractivenessBreakdown:
    """The one entry point: four area levels plus the resulting HOT/GOOD/MATCH.

    `nights` is always `(return_date - departure_date).days`, matching
    `filtering.matches_criteria`/`notification_content._stay_length` -- never
    `Offer.number_of_days`, whose meaning is provider-specific.
    """
    config = config if config is not None else DEFAULT_ATTRACTIVENESS_CONFIG
    nights = (
        (offer.return_date - offer.departure_date).days
        if offer.departure_date is not None and offer.return_date is not None
        else None
    )
    value = classify_value(offer.price_per_person, nights, config)
    hotel_quality = classify_hotel_quality(offer, provider_ratings, config)
    airport = classify_airport(offer.departure_airport, config)
    board = classify_board(offer.board_type, config)
    category = _final_category((value, hotel_quality, airport, board))
    return AttractivenessBreakdown(value, hotel_quality, airport, board, category)
