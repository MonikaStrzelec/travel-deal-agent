"""Wakacje.pl listing-only provider: HTTP GET, robots-checked, no Playwright.

The listing page's embedded `__NEXT_DATA__` carries full, structured offer data
via plain HTTP -- no client-side JS execution is required. The bare (query-less,
robots-legal) detail page carries no offer-specific data at all, so unlike
ITAKA's `itaka_details.py` there is no detail-confirmation stage here;
`price_is_complete` stays `False` unconditionally (see
`wakacje_data.normalize_offer`). This is not what keeps every Wakacje.pl offer
out of an alert -- `filtering.matches()` accepts this provider's listing price
via `filters["accept_incomplete_price_from"]`, a deliberate, per-provider
business decision made once the price semantics were confirmed (see
`wakacje_data.py`).

## The confirmed combined search query

This provider fetches a single, site-generated search query combining
flight-only transport, board types (AI/HB/ZO/FB), minimum 3 stars, minimum
rating 8.0, all four confirmed departure airports simultaneously,
cheapest-first sort, and the per-person price view (`CONFIRMED_SEARCH_QUERY`
below) -- one request surfacing results across every filter dimension at
once, pre-sorted ascending, instead of one baseline plus one separate request
per airport.

The confirmed URL also included a price cap (`do-1500zl`). That token is
excluded here: it matches `Disallow: /*?do-*` in the site's own robots.txt.
Dropping it changes nothing business-wise: the per-person price cap is, and
always was, enforced client-side by `filtering.matches_criteria`, independent
of what this provider fetches.

The site-generated page-2 link for this query is `?str-2,<same query>` -- the
same page-number-plus-filter comma shape already used for single-airport
pagination. `robots_policy`'s `/*,*/ ` disallow rule does not match this
shape: that rule requires a `/` after the comma, which this query string
never has.

## Price semantics: this query returns price PER PERSON, not total

This query includes `za-osobe` (the site's own "average per person" price-view
toggle); the raw `price` field is per person, not the party total -- see
`wakacje_data.py` for the corresponding parser handling. Since this provider
always uses this one query, the parser's price handling is unconditional, not
a per-call flag.

This provider never opens a detail page, never uses Playwright, never calls
an API endpoint directly. It combines multiple filter dimensions in one URL --
normally avoided in this project because robots.txt disallows comma-joined
*category-path* segments (`/*,*/ ` with a trailing `/`) -- but this exact
query string is itself site-generated, and its comma-joined shape (all
query-string, no further `/`) does not match that disallow rule either (same
reasoning as the pagination shape above). `filtering.matches()` remains the
sole authority for business eligibility; this query is an efficiency and
relevance improvement, never a substitute for it.

Transient network resilience: `fetch()` handles exactly two exception types
locally, never more: the builtin `TimeoutError` (a real socket/SSL read
timeout) and `urllib.error.URLError` (DNS/connection-level failures) raised by
a single `budget.get(...)` call -- never retried, matching this project's
other adapters' "no automatic retry" policy. A transient error on any page
stops further pagination but keeps every offer already parsed from earlier
pages. Robots violations, non-200 statuses (surfaced by `RequestBudget.get()`
as `ValueError`), request-budget/cycle-deadline exhaustion, and any
schema/CAPTCHA-shaped parsing failure are never caught here -- they still
fail the whole `fetch()` call.

Known limitation: this query string's pagination was only directly confirmed
for page 2 (the real on-page link). Pages 3+ extrapolate that confirmed
`str-<n>,<query>` shape to further page numbers, never separately
re-verified page-by-page.
"""

import logging
import time
from collections.abc import Callable
from datetime import datetime
from urllib.error import URLError

from ..config_types import FilterConfig, ProviderConfig
from ..models import Offer, utc_now
from .base import Provider
from .http import RequestBudget, Transport, UrllibTransport
from .robots import robots_policy
from .wakacje_data import parse_listing

logger = logging.getLogger(__name__)
BASE = "https://www.wakacje.pl"
LISTING_PATH = "/wczasy/"

# The one confirmed, robots-legal combined search query (see module docstring for
# the exact evidence trail). Order matches the site-generated URL exactly --
# nothing reordered, nothing added, nothing guessed:
#   samolotem                            -- transportType: flight only
#   all-inclusive,HB,ZO,FB               -- cateringList codes 1,2,5,6 (AI/HB/ZO/FB)
#   3-gwiazdkowe                         -- objectCategoryList: >=3 stars
#   ocena-8                              -- ratingList: >=8.0 (native 0-10 scale)
#   z-katowic,z-lodzi,z-warszawy,z-wroclawia -- all 4 confirmed target airports at once
#   tanio                                -- order: cheapest first
#   za-osobe                             -- priceType: average per person
# `do-1500zl` (price cap) was in the site-generated URL but is deliberately
# excluded: it matches robots.txt's `Disallow: /*?do-*`. The per-person price cap
# is enforced client-side by `filtering.matches_criteria` regardless.
CONFIRMED_SEARCH_QUERY = (
    "samolotem,all-inclusive,HB,ZO,FB,3-gwiazdkowe,ocena-8,"
    "z-katowic,z-lodzi,z-warszawy,z-wroclawia,tanio,za-osobe"
)

# Only page 1 uses this exact suffix; the site's own real page-2 link omits it
# (evidently a UI-navigation tracking parameter, not part of the actual
# filter/sort effect) -- each page uses exactly what was confirmed for it,
# nothing inferred beyond that.
_PAGE_1_SUFFIX = "&src=fromFilters"


class WakacjeProvider(Provider):
    name = "wakacje.pl"

    def __init__(
        self,
        configuration: ProviderConfig,
        filters: FilterConfig,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.configuration = configuration
        # `filters` is required (registry.py raises "requires shared business
        # filters" without it), matching every other provider's constructor
        # contract, even though this provider's query is a fixed, confirmed
        # constant rather than one derived from `filters["airports"]` --
        # eligibility is still decided solely by `filtering.matches()`.
        self.filters = filters
        self.transport = transport or UrllibTransport()
        self.clock = clock
        self.sleep = sleep
        self.wall_clock = wall_clock

    def fetch(self) -> list[Offer]:
        cfg = self.configuration
        budget = RequestBudget(
            self.transport,
            cfg.get("max_requests", 4),
            cfg.get("timeout_seconds", 15),
            cfg.get("cycle_seconds", 60),
            cfg.get("request_gap_seconds", 5),
            self.clock,
            self.sleep,
        )
        max_pages = cfg.get("max_pages", 2)
        robots = budget.get(BASE + "/robots.txt").text

        now = self.wall_clock()
        offers: list[Offer] = []

        page = 1
        while max_pages is None or page <= max_pages:
            if budget.calls >= budget.max_requests:
                logger.warning("Wakacje.pl partial coverage: request budget reached")
                break
            if page == 1:
                query = CONFIRMED_SEARCH_QUERY + _PAGE_1_SUFFIX
            else:
                query = f"str-{page},{CONFIRMED_SEARCH_QUERY}"
            path_and_query = f"{LISTING_PATH}?{query}"
            budget.gap = max(budget.gap, robots_policy(robots, path_and_query))
            try:
                body = budget.get(BASE + path_and_query).text
            except (TimeoutError, URLError) as exc:
                logger.warning(
                    "Wakacje.pl partial coverage: combined search page %s skipped after a "
                    "transient network error (%s); no retry, stopping pagination",
                    page,
                    exc,
                )
                break
            offers.extend(parse_listing(body, now))
            page += 1

        logger.info(
            "Wakacje.pl: %s offers from the confirmed combined search query, up to %s page(s)",
            len(offers),
            max_pages,
        )
        return offers
