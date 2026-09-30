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
    watchlist_max_pages: NotRequired[int]
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
    cache_ttl_hours: int


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


@with_config(ConfigDict(extra="forbid"))
class HotelWatchlistEntry(TypedDict):
    """One independently searched hotel, entirely separate from `FilterConfig`.

    A watched hotel is a specific property chosen explicitly,
    not one discovered via the generic quality rules in `filters` -- so it
    has its own price ceiling, stay-length and airport rules, and is never
    subject to `filters["max_price"]` or the board/rating/star rules.

    Configured in its own file, `hotel_watchlist.json` (see
    `config.load_hotel_watchlist`), never in `config.json` -- swapping,
    adding or removing a watched hotel is a `hotel_watchlist.json` edit only,
    never a code change (`@with_config(extra="forbid")` here catches a typo'd
    field the same way `AppConfig` does for `config.json`).

    `aliases` lists reasonable spelling variants a provider might return; `name`
    itself is always also accepted as an alias (see `watchlist.py`), so listing
    it again in `aliases` is optional. `country` (ISO alpha-2), when set, is an
    extra safeguard against matching a different hotel that happens to share a
    similar name in a different country; an offer with no country data is
    never rejected because of it (fails open, like elsewhere in this project).

    `provider_listings`, when set, maps a provider name (e.g. `"wakacje.pl"`)
    to that provider's own already-resolved, dedicated listing URL for this
    exact hotel -- e.g. found once by manually searching the hotel's name on
    that provider's own site and following the redirect to its hotel page.
    This project never derives or guesses that URL itself: a provider's own
    hotel-name -> hotel-ID resolution is typically an autocomplete/AJAX
    endpoint, commonly disallowed by that site's own robots.txt (see
    `providers/wakacje.py`'s "Hotel watchlist" docstring section for the
    concrete reasoning). Only `wakacje.pl` is read from this mapping today;
    a provider absent from it simply gets no *URL-based* targeted fetch from
    here -- ITAKA and TUI instead reach a watched hotel their own way,
    without a `provider_listings` URL (see `itaka.py`'s widened
    `_detail_shortlist` and `tui.py`'s `fetch_watchlist_offers`).
    """

    name: str
    aliases: list[str]
    country: NotRequired[str | None]
    people: int
    max_price_per_person: str
    min_nights: NotRequired[int | None]
    max_nights: NotRequired[int | None]
    airports: list[str]
    provider_listings: NotRequired[dict[str, str]]
    provider_destinations: NotRequired[dict[str, str]]


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
    price_drop_min_amount: str
    price_drop_min_percent: float
