"""Typed configuration contracts, validated at the JSON boundary."""

from __future__ import annotations

from pydantic import ConfigDict, with_config
from typing_extensions import NotRequired, TypedDict


class RatingScale(TypedDict):
    min: float
    max: float


class PriceBand(TypedDict):
    min_price: str
    max_price: str
    min_rating: float
    max_inclusive: NotRequired[bool]


class RatingRule(TypedDict):
    """A provider's native rating rule.

    `min_rating` is one price-independent threshold; when it is set,
    `price_bands` must be empty. Otherwise `price_bands` holds explicit
    price-dependent thresholds.
    """

    enabled: bool
    scale: RatingScale | None
    price_bands: list[PriceBand]
    min_rating: NotRequired[float]


class BoardPriceBand(TypedDict):
    min_price: str
    max_price: str
    min_board: str
    max_inclusive: NotRequired[bool]


class FilterConfig(TypedDict):
    people: int
    max_price: str
    currency: str
    airports: list[str]
    min_stars: float
    allowed_boards: list[str]
    board_price_bands: list[BoardPriceBand]
    min_nights: int | None
    max_nights: int | None
    provider_ratings: dict[str, RatingRule]
    accept_incomplete_price_from: NotRequired[list[str]]


class RankingConfig(TypedDict):
    airport_priority: list[str]
    airport_groups: NotRequired[list[list[str]]]
    board_scores: dict[str, float]
    weights: dict[str, float]
    review_count_cap: int
    star_scale_max: float
    provider_ratings: NotRequired[dict[str, RatingRule]]
    external_scale: NotRequired[RatingScale]
    external_sources: NotRequired[dict[str, ExternalSourceConfig]]


class ProviderConfig(TypedDict):
    enabled: bool
    interval_seconds: int
    interval_min_seconds: NotRequired[int]
    interval_max_seconds: NotRequired[int]
    max_pages: NotRequired[int | None]
    max_requests: NotRequired[int]
    max_detail_requests: NotRequired[int]
    timeout_seconds: NotRequired[int]
    cycle_seconds: NotRequired[int]
    request_gap_seconds: NotRequired[int]
    max_offers: NotRequired[int]
    max_analyzed_cards: NotRequired[int]
    max_scrolls: NotRequired[int]
    debug: NotRequired[bool]


class ExternalSourceConfig(TypedDict):
    enabled: bool
    scale: RatingScale
    min_confidence: float
    rating_weight: float
    reviews_weight: float


class ExternalConfig(TypedDict):
    enabled: bool
    sources: NotRequired[dict[str, ExternalSourceConfig]]
    max_candidates: int
    scale: RatingScale


class ValueThresholds(TypedDict):
    """PLN amounts as strings, like every other monetary config value (Decimal-exact)."""

    strong_max_price_per_person_per_night: str
    normal_max_price_per_person_per_night: str


class HotelQualityThresholds(TypedDict):
    """Fractions of the provider's own rating scale (0-1), not native rating units."""

    strong_min_normalized_rating: float
    normal_min_normalized_rating: float


class AirportTiers(TypedDict):
    """Airports absent from both lists are `neutral` -- an allowed airport, not a penalty."""

    strong: list[str]
    normal: list[str]


class BoardTiers(TypedDict):
    """Canonical boards absent from every list are `neutral` (e.g. a future allowed board)."""

    strong: list[str]
    normal: list[str]
    neutral: list[str]


class AttractivenessConfig(TypedDict):
    """V0 thresholds for the presentation-only HOT/GOOD/MATCH classification.

    Independent of `RankingConfig`/`Offer.final_score` (the existing internal
    sort order) -- see `attractiveness.py`.
    """

    value: ValueThresholds
    hotel_quality: HotelQualityThresholds
    airport: AirportTiers
    board: BoardTiers


class SchedulerConfig(TypedDict):
    max_backoff_exponent: int
    idle_poll_seconds: int


class ActiveHoursConfig(TypedDict):
    """A configurable local time-of-day window in which provider scans may run.

    `active_from` is inclusive and `active_until` is exclusive, matching the
    inclusive-lower/exclusive-upper convention used by price bands elsewhere
    in this project. Both are "HH:MM" 24-hour local times in `timezone`. A
    window where `active_from` is greater than `active_until` wraps past
    midnight (e.g. "22:00"-"06:00"). Setting `enabled` to false disables the
    gate entirely (scans are always allowed), without changing the code.
    """

    enabled: bool
    timezone: str
    active_from: str
    active_until: str


@with_config(ConfigDict(extra="forbid"))
class AppConfig(TypedDict):
    filters: FilterConfig
    ranking: RankingConfig
    providers: dict[str, ProviderConfig]
    external_verification: ExternalConfig
    scheduler: SchedulerConfig
    active_hours: ActiveHoursConfig
    alert_rearm_hours: int
    attractiveness: AttractivenessConfig
