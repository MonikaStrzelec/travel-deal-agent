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

## Hotel watchlist: a second, independent, per-hotel targeted fetch

`fetch_watchlist_offers()` is entirely separate from `fetch()` above: it never
runs as part of the standard combined-query scan, never touches
`CONFIRMED_SEARCH_QUERY`/`max_pages`, and a failure in it is isolated by the
caller (`Scheduler.run_once`) so it can never stop the standard search.

Manual inspection confirmed the site's own flow for reaching one
specific hotel: typing its name into the site's search box resolves it (via
the site's own autocomplete) to a dedicated listing page,
`https://www.wakacje.pl/wczasy/<hotel-slug>-h<hotelId>/`. That autocomplete
step itself is not reproduced here -- it is expected to live behind the
`/ajax/`/`*.json` surface robots.txt disallows outright (same reasoning as
`RECONNAISSANCE.md`'s finding for the bare `/oferty/...` detail page, sec
12.2) -- so this project never attempts to resolve a hotel name to that URL
itself. Instead, `HotelWatchlistEntry.provider_listings["wakacje.pl"]` in
`hotel_watchlist.json` carries the already-resolved URL, confirmed once by a
human the same way any other manual lookup would be, and reused here purely
as a targeted listing to fetch. Swapping to a different hotel is therefore a
`hotel_watchlist.json` edit only, never a code change, and Python never
constructs or guesses that URL.

The dedicated page still lives under `/wczasy/`, the same route family and
`__NEXT_DATA__` shape `wakacje_data.py` already parses. **Live-confirmed
2026-09-28** (two authorized, bounded GETs, no Playwright): plain HTTP
returns a populated `__NEXT_DATA__.offers.data` (9-10 real offers), the
existing `decode_next_data`/`extract_offers` parsing worked unchanged, and
`?tanio` demonstrably re-sorted the results ascending by price (unsorted
baseline vs. a strictly increasing sequence with the flag). One real offer in
the `?tanio` response had no departure airport at all (a non-flight product,
the same phenomenon already known for the general listing's own `?tanio`,
CURRENT_STATE.md sec "Sorting (tanio)") -- it is rejected by the existing
`_AIRPORT_CODE` check in `normalize_offer` and simply skipped, not a parser
gap. One real difference from `fetch()`'s page: **this URL never includes
`za-osobe`, so `price` is the party total, not per-person** (site's own
confirmed default, RECONNAISSANCE.md sec 8a.1) -- see `price_view="total"`
below and `wakacje_data.normalize_offer`'s docstring.

Sort is the confirmed `tanio` slug (`order` filter, cheapest first --
`RECONNAISSANCE.md` sec 11.3, the site's own sidebar-filter catalog), applied
as a single query-string flag exactly like every other single-flag filter
this provider already uses (`?z-wroclawia` etc.) -- never guessed, never
combined with any other flag in the same URL. One request per watched hotel
that has a `wakacje.pl` entry, plus one shared `robots.txt` read; no
pagination is attempted (unconfirmed whether the dedicated page has more than
one page of results beyond the ~9-10 offers seen so far -- not yet an issue
for a single hotel with a handful of date variants).
"""

import logging
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from urllib.error import URLError
from urllib.parse import urlsplit

from ..config_types import FilterConfig, HotelWatchlistEntry, ProviderConfig
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

# The confirmed cheapest-first sort slug for a per-hotel watchlist fetch (see
# the module docstring's "Hotel watchlist" section) -- the site's own `order`
# filter, RECONNAISSANCE.md sec 11.3. Not combined with any other flag.
_WATCHLIST_SORT_QUERY = "tanio"


def _watchlist_listing_path(url: str) -> str:
    """The confirmed dedicated hotel URL's path, plus the confirmed sort flag.

    Only the path is kept -- any query string already on the configured URL
    (e.g. a `?src=fromSearch` UI-navigation tracking parameter, the same kind
    of artifact `_PAGE_1_SUFFIX` above already documents for the standard
    search) is dropped and replaced with exactly one flag, `?tanio`. Raises if
    the configured URL is not a `https://www.wakacje.pl` URL -- config
    validation (`config._validate_hotel_watchlist`) already checks this at
    load time, but `fetch_watchlist_offers` checks it again here so a future
    caller of this function can never send a request to an unintended host.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "www.wakacje.pl":
        raise ValueError("hotel_watchlist provider_listings.wakacje.pl must be a wakacje.pl URL")
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return f"{path}?{_WATCHLIST_SORT_QUERY}"


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

    def fetch_watchlist_offers(self, entries: Sequence[HotelWatchlistEntry]) -> list[Offer]:
        """One targeted, cheapest-first fetch per watched hotel with a
        confirmed `wakacje.pl` dedicated listing URL (see the module
        docstring's "Hotel watchlist" section for the full mechanism).

        Entirely separate from `fetch()`: its own small request budget (one
        shared `robots.txt` read plus one request per watched hotel), never
        sharing or affecting `fetch()`'s own `max_pages`/`max_requests`. A
        watched hotel without a `provider_listings["wakacje.pl"]` entry is
        silently skipped here (nothing to fetch), not an error. A robots
        violation or transient network error for *one hotel's own listing*
        skips only that hotel and logs a warning. A failure reading the
        shared `robots.txt` itself (blocked, malformed, non-200) still fails
        closed and raises out of this method, exactly like `fetch()` --
        `Scheduler.run_once` isolates any call to this method as a whole, so
        even that case can never stop the standard search.
        """
        targets = [
            (entry, listing_url)
            for entry in entries
            if (listing_url := entry.get("provider_listings", {}).get(self.name))
        ]
        if not targets:
            return []
        cfg = self.configuration
        budget = RequestBudget(
            self.transport,
            1 + len(targets),
            cfg.get("timeout_seconds", 15),
            cfg.get("cycle_seconds", 60),
            cfg.get("request_gap_seconds", 5),
            self.clock,
            self.sleep,
        )
        robots = budget.get(BASE + "/robots.txt").text
        now = self.wall_clock()
        offers: list[Offer] = []
        for entry, listing_url in targets:
            try:
                path_and_query = _watchlist_listing_path(listing_url)
                budget.gap = max(budget.gap, robots_policy(robots, path_and_query))
            except ValueError as exc:
                logger.warning(
                    "Wakacje.pl watchlist listing for %r skipped (%s)", entry["name"], exc
                )
                continue
            try:
                body = budget.get(BASE + path_and_query).text
            except (TimeoutError, URLError) as exc:
                logger.warning(
                    "Wakacje.pl watchlist fetch for %r skipped after a transient network "
                    "error (%s); no retry",
                    entry["name"],
                    exc,
                )
                continue
            # No `za-osobe` flag on this URL (see `_watchlist_listing_path`),
            # so the site's own confirmed default price view applies here:
            # the party total, not per-person (live-confirmed 2026-09-28; see
            # `wakacje_data.normalize_offer`'s `price_view` docstring).
            offers.extend(parse_listing(body, now, price_view="total"))
        logger.info(
            "Wakacje.pl watchlist: %s offer(s) from %s targeted hotel listing(s)",
            len(offers),
            len(targets),
        )
        return offers
