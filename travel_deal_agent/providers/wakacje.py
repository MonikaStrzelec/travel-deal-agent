"""Wakacje.pl listing-only provider: HTTP GET, robots-checked, no Playwright.

Confirmed offline (`experiments/wakacje_pl/RECONNAISSANCE.md` sec 3, 7, 9, 12): the
listing page's embedded `__NEXT_DATA__` carries full, structured offer data via plain
HTTP -- no client-side JS execution is required. The bare (query-less, robots-legal)
detail page carries NO offer-specific data at all (sec 12.2), so unlike ITAKA's
`itaka_details.py` there is no detail-confirmation stage here; `price_is_complete`
stays `False` unconditionally (see `wakacje_data.normalize_offer`). This is no
longer what keeps every Wakacje.pl offer out of an alert -- `filtering.matches()`
accepts this provider's listing price via `filters["accept_incomplete_price_from"]`,
a deliberate, per-provider business decision made once the price semantics (total
for the queried party, not per-person) were confirmed (sec 8a.1). `price_is_complete`
itself is untouched and keeps meaning exactly what it always did: whether the price
was confirmed on a detail/checkout page, which for this source it structurally never
is.

The base scope is `/wczasy/` (confirmed live, second recon session), not
`/lastminute/`: it is the site's own general search -- its own search box literally
reads "Dowolny kierunek lub hotel" -- and returns a materially larger, differently
composed result set than the last-minute-only category. `/lastminute/` is no longer
queried.

A single-flag departure-airport filter (`?z-<city>`) was confirmed to genuinely
change server-rendered results (sec 12.1) and to make every returned offer
unambiguously priced for that one airport -- this replaces the detail-page
confirmation step other providers use for the same ambiguous-card problem. Only
airports with a slug directly confirmed *as a standalone request* are queried
(`CONFIRMED_AIRPORT_SLUGS`, now LCJ, WAW, KTW and WRO -- see its own docstring for
the exact live evidence behind each entry). WMI has no such confirmed slug and is
simply not queried on its own, though it may still turn up in the unfiltered
baseline fetch by chance. Never guessed -- this specifically includes the UI's own
Chopin-specific sub-filter (`z-warszawa-chopin`, confirmed to exist in the site's UI
but empirically returns `301` -> `/wczasy/` as an isolated request, i.e. the site
itself declines to honor it in that shape) and `z-warszawy-radom` (RDO, a real slug
seen on-page but never live-verified standalone, and not one of our configured
airports) -- both stay unconfirmed and unused.

Pagination for a confirmed airport is real and confirmed live (third recon session,
for WRO; the same mechanism is used for WAW, KTW and LCJ, not separately
re-verified page by page for each): the site's own pagination links on an
airport-filtered page are themselves
comma-joined (`?str-<n>,<confirmed-slug>`), and `robots_policy` allows them (the
`/*,*/ ` disallow rule requires a `/` after the comma, which this shape never has).
Only this one, site-generated combination (page number + the *same* single
confirmed airport slug) is used; sorting (`?tanio`) is
deliberately never queried, alone or combined -- the cheapest-sorted results were
confirmed live to be dominated by no-flight offers with no departure-airport code at
all (would fail `RawOffer`'s airport-code validation anyway), and no on-page evidence
of a legal `tanio`+airport combination was ever found. Pages per confirmed airport
are bounded by the provider's own `max_pages` (same convention as `itaka.py`; `None`
means unbounded, capped only by the shared request budget). The unfiltered baseline
fetch itself stays single-page.

This provider never opens a detail page, never uses Playwright, never calls an API
endpoint directly, and never combines more than one filter dimension in a single URL
(robots.txt disallows combining filters via a comma-joined segment ending in `/`;
sec 1, 8a.4) -- the one exception is the confirmed pagination shape above, which
combines a page number with the *same* single airport filter, not two different
filter dimensions. `filtering.matches()` remains the sole authority for business
eligibility.

Transient network resilience (RECONNAISSANCE.md sec 22-23): a real full-provider
live run hit a `TimeoutError` from the underlying socket mid-scan, and
`Scheduler._fetch()`'s all-or-nothing exception handling discarded every offer
already collected that cycle even though several airports had already succeeded.
`fetch()` now handles exactly two exception types locally, never more: the
builtin `TimeoutError` (a real socket/SSL read timeout, as observed live) and
`urllib.error.URLError` (DNS/connection-level failures) raised by a single
`budget.get(...)` call -- never retried, since this project's other adapters
(ITAKA, TUI, Rainbow) already establish "no automatic retry" as the deliberate
policy for network failures. On the baseline listing, this only logs a warning
and skips it; on a confirmed airport's page N, it stops that one airport's
remaining pages (no page N+1 attempt) and moves on to the next airport, keeping
every offer already parsed from earlier pages/airports. This is deliberately
narrow: robots violations, non-200 statuses (403/429/redirects, all surfaced by
`RequestBudget.get()` as `ValueError`), request-budget/cycle-deadline
exhaustion, and any schema/CAPTCHA-shaped parsing failure from
`parse_listing()`/`decode_next_data()`/`extract_offers()` are never caught here
-- they still fail the whole `fetch()` call, exactly as before.
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

# Confirmed, real, robots-legal single-flag departure filters. Each entry here
# was live-verified as a standalone, isolated request (not merely observed as
# a fragment of a larger, comma-joined URL): the fetch returned 200 (not a
# redirect) and every returned offer's own `departurePlaceCode` matched the
# requested airport exactly.
#
# "LCJ": "z-lodzi" -- confirmed live (RECONNAISSANCE.md sec 21): `GET
# /wczasy/?z-lodzi` returned 200 with 10/10 offers carrying
# departurePlaceCode == "LCJ", no other code in the sample. Previously only
# seen inside a multi-filter, comma-joined UI URL (sec 16, 20) -- this is its
# first standalone confirmation. LCJ has the highest business/ranking
# priority of the four confirmed airports (`config.json: ranking.airport_groups`).
#
# "WAW": "z-warszawy" -- confirmed live in a later session: `GET
# /wczasy/?z-warszawy` returned 200 with 10/10 offers carrying
# departurePlaceCode == "WAW" (zero WMI, zero RDO, zero other codes in that
# sample). This is the site's own *collective* "Warszawa" checkbox slug, not
# the narrower per-airport one exposed by its "Pokaz lotniska" sub-selector.
#
# "KTW": "z-katowic" -- confirmed live (RECONNAISSANCE.md sec 21): `GET
# /wczasy/?z-katowic` returned 200 with 10/10 offers carrying
# departurePlaceCode == "KTW", no other code in the sample. Previously only
# seen inside disallowed, comma-joined per-offer detail hrefs (sec 13d) and a
# multi-filter UI URL (sec 16, 20) -- this is its first standalone
# confirmation.
#
# "WRO": "z-wroclawia" -- confirmed via `?z-wroclawia` (RECONNAISSANCE.md
# sec 11.4, 12.1): 200, every returned offer had departurePlaceCode == "WRO".
#
# Explicitly NOT added, and not to be guessed:
# - WMI: no confirmed slug found in any reconnaissance session.
# - "z-warszawa-chopin" (the UI's Chopin-only sub-filter, confirmed to exist
#   as a real slug *inside* a larger, comma-joined, site-generated URL): as an
#   isolated, standalone single-flag request it returned `301` with
#   `Location: /wczasy/` -- the site silently drops that exact filter shape
#   rather than honoring it or canonicalizing it, unlike "z-warszawy" and
#   "z-wroclawia". Not usable the way this dict's entries are used.
# - "z-warszawy-radom" (Warszawa-Radom/RDO): a real, distinct slug seen on-page
#   in earlier reconnaissance, but never live-verified as a standalone request
#   the way the entries below were, and RDO is not one of our configured
#   airports (`filters.airports`) -- out of scope, not added.
CONFIRMED_AIRPORT_SLUGS: dict[str, str] = {
    "LCJ": "z-lodzi",
    "WAW": "z-warszawy",
    "KTW": "z-katowic",
    "WRO": "z-wroclawia",
}


# Re-exported for existing imports/tests (`from .wakacje import robots_policy`);
# the implementation now lives in .robots, shared with itaka.py and tui.py.
__all__ = ["robots_policy"]


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
        confirmed = [a for a in filters["airports"] if a in CONFIRMED_AIRPORT_SLUGS]
        unconfirmed = [a for a in filters["airports"] if a not in CONFIRMED_AIRPORT_SLUGS]
        if unconfirmed:
            logger.info(
                "Wakacje.pl: no confirmed departure-airport slug for %s; each is only "
                "reachable via the unfiltered baseline listing by chance, never "
                "deliberately queried (see RECONNAISSANCE.md sec 8b.6, 9)",
                unconfirmed,
            )
        self.airports = confirmed
        self.transport = transport or UrllibTransport()
        self.clock = clock
        self.sleep = sleep
        self.wall_clock = wall_clock

    def fetch(self) -> list[Offer]:
        cfg = self.configuration
        budget = RequestBudget(
            self.transport,
            cfg.get("max_requests", 5),
            cfg.get("timeout_seconds", 15),
            cfg.get("cycle_seconds", 60),
            cfg.get("request_gap_seconds", 5),
            self.clock,
            self.sleep,
        )
        max_pages = cfg.get("max_pages", 2)
        robots = budget.get(BASE + "/robots.txt").text
        budget.gap = max(budget.gap, robots_policy(robots, LISTING_PATH))

        now = self.wall_clock()
        offers: list[Offer] = []

        robots_policy(robots, LISTING_PATH)
        try:
            baseline = budget.get(BASE + LISTING_PATH).text
        except (TimeoutError, URLError) as exc:
            logger.warning(
                "Wakacje.pl partial coverage: baseline listing skipped after a transient "
                "network error (%s); continuing with confirmed airport scans",
                exc,
            )
        else:
            offers.extend(parse_listing(baseline, now))

        for airport in self.airports:
            slug = CONFIRMED_AIRPORT_SLUGS[airport]
            page = 1
            while max_pages is None or page <= max_pages:
                if budget.calls >= budget.max_requests:
                    logger.warning("Wakacje.pl partial coverage: request budget reached")
                    break
                query = slug if page == 1 else f"str-{page},{slug}"
                path_and_query = f"{LISTING_PATH}?{query}"
                robots_policy(robots, path_and_query)
                try:
                    body = budget.get(BASE + path_and_query).text
                except (TimeoutError, URLError) as exc:
                    logger.warning(
                        "Wakacje.pl partial coverage: airport %s page %s skipped after a "
                        "transient network error (%s); no retry, moving to the next airport",
                        airport,
                        page,
                        exc,
                    )
                    break
                offers.extend(parse_listing(body, now, requested_departure_airport=airport))
                page += 1

        logger.info(
            "Wakacje.pl: %s offers from the unfiltered listing plus %s confirmed "
            "airport-filtered listing(s), up to %s page(s) each",
            len(offers),
            len(self.airports),
            max_pages,
        )
        return offers
