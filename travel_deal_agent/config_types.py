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
    enabled: bool
    scale: RatingScale | None
    price_bands: list[PriceBand]


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
    country_min_stars: dict[str, float]
    allowed_boards: list[str]
    board_price_bands: list[BoardPriceBand]
    min_days: int
    max_days: int | None
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


class SchedulerConfig(TypedDict):
    max_backoff_exponent: int
    idle_poll_seconds: int


@with_config(ConfigDict(extra="forbid"))
class AppConfig(TypedDict):
    filters: FilterConfig
    ranking: RankingConfig
    providers: dict[str, ProviderConfig]
    external_verification: ExternalConfig
    scheduler: SchedulerConfig
    price_drop_pln: str
