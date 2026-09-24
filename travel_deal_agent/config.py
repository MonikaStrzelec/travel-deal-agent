"""Load typed configuration and validate business constraints at startup."""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from pydantic import TypeAdapter

from .active_hours import validate_active_hours
from .alerts import PriceDropThreshold
from .attractiveness import validate_attractiveness_config
from .boards import BOARD_ORDER, CANONICAL_BOARDS
from .config_types import (
    ActiveHoursConfig,
    AppConfig,
    AttractivenessConfig,
    ExternalConfig,
    FilterConfig,
    ProviderConfig,
    RankingConfig,
    SchedulerConfig,
)
from .ratings import validate_rating_rules

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    """Validated settings shared by application services."""

    filters: FilterConfig
    ranking: RankingConfig
    providers: dict[str, ProviderConfig]
    alert_rearm_after: timedelta
    database: Path
    log_level: str
    external_verification: ExternalConfig
    scheduler: SchedulerConfig
    active_hours: ActiveHoursConfig
    attractiveness: AttractivenessConfig
    price_drop_threshold: PriceDropThreshold


@dataclass(frozen=True)
class TelegramConfig:
    """Telegram Bot API credentials; never logged or included in error messages."""

    bot_token: str
    chat_id: str


def load_telegram_config(env: Mapping[str, str] | None = None) -> TelegramConfig | None:
    """Read optional Telegram credentials; both variables are required together.

    Returns None when neither is set, so callers fall back to ConsoleNotifier.
    """
    source = env if env is not None else os.environ
    token = source.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = source.get("TELEGRAM_CHAT_ID", "").strip()
    if not token and not chat_id:
        return None
    if not token or not chat_id:
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set")
    return TelegramConfig(token, chat_id)


def validate_filters(filters: FilterConfig) -> None:
    """Reject invalid limits before processing any offers."""
    if filters["people"] < 1:
        raise ValueError("People must be positive")
    minimum_nights, maximum_nights = filters["min_nights"], filters["max_nights"]
    if minimum_nights is not None and minimum_nights < 1:
        raise ValueError("min_nights must be null or positive")
    if maximum_nights is not None and maximum_nights < 1:
        raise ValueError("max_nights must be null or positive")
    if (
        minimum_nights is not None
        and maximum_nights is not None
        and maximum_nights < minimum_nights
    ):
        raise ValueError("max_nights must be null or at least min_nights")
    if not filters["airports"] or not filters["currency"]:
        raise ValueError("Airports and currency are required")
    if not 0 < filters["min_stars"] <= 5:
        raise ValueError("Star threshold must be between 0 and 5")
    if not filters["allowed_boards"] or not set(filters["allowed_boards"]) <= CANONICAL_BOARDS - {
        "RO"
    }:
        raise ValueError("Allowed boards must include breakfast; unknown boards are forbidden")
    budget = positive_decimal(filters["max_price"])
    previous_end = Decimal("0")
    previous_inclusive = False
    for band in filters["board_price_bands"]:
        start, end = Decimal(band["min_price"]), Decimal(band["max_price"])
        if not start.is_finite() or not end.is_finite() or start < 0 or end <= start:
            raise ValueError("Invalid meal price band bounds")
        if start != previous_end or previous_inclusive:
            raise ValueError("Meal price bands must be contiguous and non-overlapping from zero")
        if band["min_board"] not in CANONICAL_BOARDS - {"RO"}:
            raise ValueError("Meal price bands require breakfast or better")
        previous_end = end
        previous_inclusive = band.get("max_inclusive", False)
    if previous_end < budget or (previous_end == budget and not previous_inclusive):
        raise ValueError("Meal price bands must cover the entire price budget")
    validate_rating_rules(filters["provider_ratings"])


def positive_decimal(value: str) -> Decimal:
    """Parse a positive finite monetary amount without float rounding."""
    number = Decimal(value)
    if not number.is_finite() or number <= 0:
        raise ValueError("Price limits must be finite and positive")
    return number


def validate_options(raw: AppConfig) -> None:
    """Validate ranking, scheduling and external verification settings."""
    if type(raw["alert_rearm_hours"]) is not int or raw["alert_rearm_hours"] <= 0:
        raise ValueError("alert_rearm_hours must be a positive whole number of hours")
    drop_amount = Decimal(raw["price_drop_min_amount"])
    if not drop_amount.is_finite() or drop_amount < 0:
        raise ValueError("price_drop_min_amount must be a nonnegative amount")
    drop_percent = raw["price_drop_min_percent"]
    if not math.isfinite(drop_percent) or not 0 <= drop_percent <= 1:
        raise ValueError("price_drop_min_percent must be between 0 and 1")
    ranking = raw["ranking"]
    expected = {
        "price",
        "airport",
        "rating",
        "reviews",
        "stars",
        "google_rating",
        "google_reviews",
        "board",
    }
    if set(ranking["weights"]) != expected:
        raise ValueError("Ranking must specify all eight weights")
    if any(not math.isfinite(v) or v < 0 for v in ranking["weights"].values()):
        raise ValueError("Ranking weights must be finite and nonnegative")
    if not ranking["airport_priority"] or ranking["review_count_cap"] <= 0:
        raise ValueError("Ranking requires airports and a positive review cap")
    if not math.isfinite(ranking["star_scale_max"]) or ranking["star_scale_max"] <= 0:
        raise ValueError("Invalid ranking star scale")
    if set(ranking["board_scores"]) != CANONICAL_BOARDS:
        raise ValueError("Configure scores for every canonical board")
    if any(not math.isfinite(v) or not 0 <= v <= 1 for v in ranking["board_scores"].values()):
        raise ValueError("Board scores must be finite values in [0, 1]")
    if any(
        ranking["board_scores"][a] >= ranking["board_scores"][b]
        for a, b in zip(BOARD_ORDER, BOARD_ORDER[1:], strict=False)
    ):
        raise ValueError("Meal ranking scores must increase from RO through UAI")
    if "airport_groups" in ranking:
        groups = ranking["airport_groups"]
        flattened = [airport for group in groups for airport in group]
        if (
            not groups
            or any(not group for group in groups)
            or len(flattened) != len(set(flattened))
            or set(flattened) != set(ranking["airport_priority"])
        ):
            raise ValueError("Airport groups must partition airport_priority")
    external = raw["external_verification"]
    validate_rating_rules(
        {"google": {"enabled": False, "scale": external["scale"], "price_bands": []}}
    )
    for name, policy in external.get("sources", {}).items():
        if not name or name != name.strip().lower():
            raise ValueError("External source names must be normalized")
        validate_rating_rules(
            {name: {"enabled": False, "scale": policy["scale"], "price_bands": []}}
        )
        if not math.isfinite(policy["min_confidence"]) or not 0 <= policy["min_confidence"] <= 1:
            raise ValueError("Invalid match confidence threshold")
        if any(
            not math.isfinite(weight) or weight < 0
            for weight in (policy["rating_weight"], policy["reviews_weight"])
        ):
            raise ValueError("Invalid external ranking weight")
    if external["max_candidates"] < 0:
        raise ValueError("Invalid external candidate limit")
    for provider_name, provider in raw["providers"].items():
        if provider["interval_seconds"] < 1:
            raise ValueError("Provider intervals must be positive")
        has_min = "interval_min_seconds" in provider
        has_max = "interval_max_seconds" in provider
        if has_min != has_max:
            raise ValueError("interval_min_seconds and interval_max_seconds must be set together")
        if has_min and not (
            0 < provider["interval_min_seconds"] <= provider["interval_max_seconds"]
        ):
            raise ValueError(
                "interval_min_seconds must be positive and at most interval_max_seconds"
            )
        pages = provider.get("max_pages", 2)
        detail_requests = provider.get("max_detail_requests", 1)
        if detail_requests < 0:
            raise ValueError("max_detail_requests must be nonnegative")
        if provider_name == "tui" and detail_requests > 3:
            # Each TUI detail request is one full Playwright navigation (heavier
            # than an HTTP request); this is the simple per-cycle browser-
            # navigation budget: 1 fixed listing navigation + at most 3 detail
            # navigations. Raising it needs a deliberate config change, not a
            # silent default.
            raise ValueError(
                "TUI max_detail_requests must be at most 3 (browser navigation budget)"
            )
        if pages is not None and pages < 1:
            raise ValueError("max_pages must be positive or null")
        if provider_name == "tui":
            # Same simple browser-navigation budget as max_detail_requests above:
            # unlike ITAKA's HTTP pagination, `null` (unlimited) is not supported
            # here -- every TUI page is a full Playwright navigation, and this
            # provider must never traverse an unbounded number of them.
            if pages is None:
                raise ValueError("TUI max_pages must not be null (no unlimited pagination)")
            if pages > 3:
                raise ValueError("TUI max_pages must be at most 3 (browser navigation budget)")
        if (
            min(
                provider.get("max_requests", 3),
                provider.get("timeout_seconds", 15),
                provider.get("cycle_seconds", 60),
                provider.get("request_gap_seconds", 5),
            )
            < 1
        ):
            raise ValueError("HTTP limits must be positive")
        if provider_name == "itaka" and pages is not None:
            # One robots.txt fetch, up to `pages` listing pages, up to
            # `detail_requests` detail confirmations, all sharing one budget
            # (itaka.py fetch()). A budget too small to ever reach a detail
            # request would make max_detail_requests declared but unreachable.
            required = 1 + pages + detail_requests
            if provider.get("max_requests", 3) < required:
                raise ValueError(
                    "ITAKA max_requests must cover robots.txt + max_pages listing pages + "
                    "max_detail_requests detail requests"
                )
    if (
        raw["scheduler"]["idle_poll_seconds"] < 1
        or not 0 <= raw["scheduler"]["max_backoff_exponent"] <= 20
    ):
        raise ValueError("Invalid scheduler timing settings")
    validate_active_hours(raw["active_hours"])
    validate_attractiveness_config(raw["attractiveness"])


def load_settings() -> Settings:
    """Read environment overrides and strictly validate the JSON configuration."""
    load_dotenv(ROOT / ".env", override=False)
    config_path = ROOT / os.getenv("TDA_CONFIG", "config.json")
    raw = TypeAdapter(AppConfig).validate_json(
        config_path.read_text(encoding="utf-8-sig"), strict=True
    )
    # A registered provider without its own provider_ratings entry (e.g. TUI --
    # MVP decision: no TUI hard rating filter; use rating/review data in
    # ranking. See experiments/tui/CURRENT_STATE.md) defaults to a disabled
    # rule, exactly like an explicit `{"enabled": false, ...}` entry (e.g.
    # "mock" already has one).
    # This never invents a quality threshold and never touches config.json; it
    # only prevents `rating_matches()` from treating "no rule configured yet" the
    # same as "reject every offer from this provider", which `rules.get(name)
    # is None -> False` would otherwise do. A provider absent from `providers`
    # entirely (a typo or a name nobody registered) is unaffected and still
    # fails closed via that same lookup.
    for name in raw["providers"]:
        raw["filters"]["provider_ratings"].setdefault(
            name, {"enabled": False, "scale": None, "price_bands": []}
        )
    if "ITAKA_MAX_PAGES" in os.environ:
        if "itaka" not in raw["providers"]:
            raise ValueError("ITAKA_MAX_PAGES requires provider configuration")
        raw["providers"]["itaka"]["max_pages"] = int(os.environ["ITAKA_MAX_PAGES"])
    validate_filters(raw["filters"])
    validate_options(raw)
    if "rainbow" in raw["providers"]:
        from .providers.rainbow_config import Limits, SearchPlan

        Limits.from_config(raw["providers"]["rainbow"])
        if raw["providers"]["rainbow"]["enabled"]:
            SearchPlan.from_filters(raw["filters"])
    ranking = raw["ranking"]
    ranking["provider_ratings"] = raw["filters"]["provider_ratings"]
    ranking["external_scale"] = raw["external_verification"]["scale"]
    ranking["external_sources"] = raw["external_verification"].get("sources", {})
    level = os.getenv("TDA_LOG_LEVEL", "INFO").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("Invalid TDA_LOG_LEVEL")
    return Settings(
        raw["filters"],
        ranking,
        raw["providers"],
        timedelta(hours=raw["alert_rearm_hours"]),
        ROOT / os.getenv("TDA_DATABASE", "data/offers.sqlite3"),
        level,
        raw["external_verification"],
        raw["scheduler"],
        raw["active_hours"],
        raw["attractiveness"],
        PriceDropThreshold(
            Decimal(raw["price_drop_min_amount"]), Decimal(str(raw["price_drop_min_percent"]))
        ),
    )
