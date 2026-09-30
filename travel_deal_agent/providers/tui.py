"""TUI hybrid provider: HTTP robots check, then one passive Playwright capture.

The production data source is the client-side
`.../api/services/tui-search/api/search/offers` response, observed by passively
listening after opening a `build_search_path` URL in a real browser -- never fetched
directly (see `tui_browser`). The SSR `/wypoczynek/wyniki-wyszukiwania-samolot`
route renders only a loading skeleton with no offers via plain HTTP, so that path
is not used here. `filtering.matches()` downstream remains the sole authority for
eligibility (stay length, price/person, etc.) regardless of what upstream returns.

Pagination is bounded by the *live-observed* `pagination.pagesCount` on page 1: at
most `max_pages` (config, default `1`, MVP value `3`) pages are fetched -- this
provider never fetches more pages than the site itself reports exist, and never
guesses a page's content without requesting it.

After the (possibly multi-page) listing capture, at most `max_detail_requests`
(default 1) offers that already pass every listing-known filter
(`filtering.matches_criteria`; see `_shortlist`) get one additional, passive
detail-page navigation to confirm real-time price/availability (see
`tui_price.confirm_realtime_price`), mirroring ITAKA's bounded detail-
confirmation budget. With only one detail request per cycle to spend, the
shortlist picks the most promising candidate rather than simply the cheapest
one that clears the bar: candidates are ordered by their listing-time
`attractiveness.classify_offer` category (HOT, then GOOD, then MATCH) and only
within the same category by ascending `price_per_person` (see `_shortlist`).
`price_is_complete` is set to `True` only for a structurally confirmed
charter-flight package tour whose mandatory TFG+TFP fund matches the
officially confirmed rate -- see `tui_price`'s module docstring. Every other
outcome (non-charter, unavailable, ambiguous, malformed) leaves the offer
unconfirmed (`CHARTER_PACKAGE_NOT_CONFIRMED` or the specific rejection reason)
without ever failing the whole cycle.

**Browser-navigation budget per cycle** (TUI is heavier than an HTTP-only source
like Wakacje.pl -- every request here is a full Playwright page load, not a
plain HTTP GET): at most `max_pages` listing navigations (page 1 is always
fetched; `config.py` caps `max_pages` at 3 for this provider) plus at most
`max_detail_requests` detail navigations (capped at 3) -- at most 4
navigations per cycle for the shipped config (`max_pages: 3`,
`max_detail_requests: 1`). A transient timeout fetching page 2 or 3 keeps every
page already fetched and simply stops further pagination for that cycle
(`TuiTimeout` only); a block, ambiguous response or schema/robots failure on
any page still fails the whole cycle -- pagination never weakens those
existing fail-closed checks. `robots.txt` (one plain HTTP request, not a
navigation) and `timeout_seconds` bound each individual navigation's own
duration.

**Aggregate cycle deadline:** `cycle_seconds` (config, default `90`) bounds
the whole cycle's wall-clock time, checked with an injected monotonic `clock`
before every Playwright navigation *after* the first (page 1 is always
fetched, mirroring ITAKA/Rainbow's own always-fetched first request). This is
independent of each navigation's own `timeout_seconds` -- a slow but
individually successful sequence of navigations can still exceed the
aggregate budget. When exceeded, the affected step is skipped exactly as if
its own transient `TuiTimeout` had fired: pagination keeps every page already
fetched and stops requesting more; detail confirmation leaves the remaining
candidates unconfirmed (`price_is_complete=False`) without attempting their
navigation. This never catches `TuiBlocked`/`TuiStructureError`/robots
failures, which still fail the whole cycle exactly as before.

## Hotel watchlist: a second, independent search query

`fetch()`'s own `amountRange` facet (`tui_query._price_ceiling`, derived from
`filters["max_price"]`) is sent straight to TUI's own search API -- a
query-level block, not a client-side filter -- so a watched hotel priced
above the standard 1500 PLN/person cap can never appear in `fetch()`'s own
results at all, unlike ITAKA (blocked only at its own detail-confirmation
shortlist, fixable without any extra request -- see `itaka.py`). `TUI
therefore needs its own second, small request budget for the watchlist,
exactly like wakacje.pl's dedicated per-hotel fetch: see
`fetch_watchlist_offers()` and `tui_query.build_watchlist_search_path`.

The watchlist query is price-sorted (20 offers per page) and reads page 1 plus
up to `watchlist_max_pages` (config, 1..3, default 1) pages in total -- never
all `pagesCount` pages, so coverage of the upper price band is best-effort.
"""

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from ..attractiveness import DEFAULT_ATTRACTIVENESS_CONFIG, classify_offer
from ..config_types import AttractivenessConfig, FilterConfig, HotelWatchlistEntry, ProviderConfig
from ..filtering import matches_criteria
from ..models import Offer, utc_now
from ..watchlist import hotel_identity_matches
from .base import Provider
from .http import Transport, UrllibTransport
from .robots import robots_policy
from .tui_browser import capture_offer_price, capture_search_offers
from .tui_data import extract_search_response_pagination, parse_search_response
from .tui_errors import TuiError, TuiTimeout
from .tui_price import confirm_realtime_price
from .tui_query import (
    PATH,
    build_search_path,
    build_watchlist_search_path,
    split_watchlist_entries,
)

logger = logging.getLogger(__name__)
BASE = "https://www.tui.pl"

# `robots.txt` is fetched once per provider "cycle" (`fetch()` followed by
# `fetch_watchlist_offers()` seconds later) and re-fetched after this TTL, so a
# long-lived provider instance never trusts a stale copy for hours.
ROBOTS_CACHE_TTL_SECONDS = 300.0

# Lower rank sorts first: HOT is the most promising, MATCH merely eligible.
# `attractiveness.classify_offer` never returns anything else.
_ATTRACTIVENESS_RANK = {"HOT": 0, "GOOD": 1, "MATCH": 2}


def _dedupe_by_offer_id(offers: list[Offer]) -> list[Offer]:
    """Keep the first occurrence of each offer_id across pages.

    Pages are not expected to overlap, but this stays a defensive guard against
    any overlap (e.g. sorting shifting between two page fetches) -- it never
    merges different offer_ids, so distinct dates/variants of the same hotel
    are never collapsed into one entry.
    """
    seen: set[str] = set()
    result: list[Offer] = []
    for offer in offers:
        if offer.offer_id in seen:
            continue
        seen.add(offer.offer_id)
        result.append(offer)
    return result


class TuiProvider(Provider):
    name = "tui"

    def __init__(
        self,
        configuration: ProviderConfig,
        filters: FilterConfig,
        transport: Transport | None = None,
        capture: Callable[[str, float], str] = capture_search_offers,
        capture_price: Callable[[str, float], str] = capture_offer_price,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], datetime] = utc_now,
        clock: Callable[[], float] = time.monotonic,
        attractiveness: AttractivenessConfig | None = None,
    ) -> None:
        self.configuration = configuration
        self.filters = filters
        self.path = build_search_path(filters)
        self.transport = transport or UrllibTransport()
        self.capture = capture
        self.capture_price = capture_price
        self.sleep = sleep
        self.wall_clock = wall_clock
        self.clock = clock
        self.attractiveness = (
            attractiveness if attractiveness is not None else DEFAULT_ATTRACTIVENESS_CONFIG
        )
        self._robots_cache: tuple[datetime, str] | None = None

    def _robots_text(self, timeout: float) -> str:
        """Fetch `robots.txt` (fail-closed on any non-200), reusing a copy
        younger than `ROBOTS_CACHE_TTL_SECONDS` so the standard and watchlist
        fetches of one cycle share one request. Failures are never cached."""
        now = self.wall_clock()
        cached = self._robots_cache
        if cached is not None and (now - cached[0]).total_seconds() < ROBOTS_CACHE_TTL_SECONDS:
            return cached[1]
        response = self.transport.get(BASE + "/robots.txt", timeout)
        if response.status != 200:
            raise ValueError(f"TUI robots.txt HTTP {response.status}; no automatic retry")
        self._robots_cache = (now, response.text)
        return response.text

    def fetch(self) -> list[Offer]:
        cfg = self.configuration
        timeout = cfg.get("timeout_seconds", 20)
        deadline = self.clock() + cfg.get("cycle_seconds", 90)
        robots_text = self._robots_text(timeout)
        delay = robots_policy(robots_text, PATH)
        if delay:
            self.sleep(delay)
        body = self.capture(BASE + self.path, timeout)
        offers = parse_search_response(body, self.wall_clock())
        offers += self._fetch_additional_pages(body, robots_text, timeout, deadline)
        offers = _dedupe_by_offer_id(offers)
        logger.info(
            "TUI: %s offers parsed from the passively captured search/offers response(s)",
            len(offers),
        )
        return self._confirm_candidates(offers, robots_text, timeout, deadline)

    def _fetch_additional_pages(
        self, first_page_body: str, robots_text: str, timeout: float, deadline: float
    ) -> list[Offer]:
        """Fetch page 2..min(pagesCount, max_pages), never more than the site
        itself reports existing and never guessed beyond a live pagesCount."""
        max_pages = self.configuration.get("max_pages", 1)
        if max_pages is None:
            max_pages = 1  # config.py rejects null for tui; belt-and-braces here too
        pagination = extract_search_response_pagination(first_page_body)
        pages_count = pagination.get("pagesCount")
        total_pages = pages_count if isinstance(pages_count, int) and pages_count > 0 else 1
        pages_to_fetch = min(total_pages, max_pages)

        offers: list[Offer] = []
        for page in range(2, pages_to_fetch + 1):
            if self.clock() >= deadline:
                logger.warning(
                    "TUI pagination stopped before page %s (cycle deadline exceeded); "
                    "keeping %s page(s) already fetched",
                    page,
                    page - 1,
                )
                break
            page_delay = robots_policy(robots_text, PATH)
            if page_delay:
                self.sleep(page_delay)
            path = build_search_path(self.filters, page=page)
            try:
                page_body = self.capture(BASE + path, timeout)
                offers += parse_search_response(page_body, self.wall_clock())
            except TuiTimeout as exc:
                logger.warning(
                    "TUI pagination stopped at page %s (timeout); keeping pages already fetched: %s",
                    page,
                    exc,
                )
                break
        return offers

    def _shortlist(self, offers: list[Offer]) -> set[str]:
        """Pick at most `max_detail_requests` offer IDs worth spending the
        scarce real-time detail budget on.

        Narrowed first to offers that already pass every listing-known filter
        (`filtering.matches_criteria`, which never looks at `price_is_complete`
        -- an incomplete price never disqualifies a candidate here, since
        confirming that exact price is the point of the detail request this
        shortlist feeds). Reusing `matches_criteria` directly means a listing
        offer with an already-known disqualifying field never spends a detail
        request that could not possibly have turned it eligible.

        The remaining candidates are then ordered by their listing-time
        `attractiveness.classify_offer` category (HOT before GOOD before
        MATCH) and only within the same category by ascending
        `price_per_person`; equal category and price keep their original
        listing order (`list.sort` is stable). This spends the scarce budget
        confirming the best-looking deal, not merely the cheapest one that
        clears the eligibility bar.
        """
        budget = self.configuration.get("max_detail_requests", 1)
        today = self.wall_clock().date()
        candidates = [
            offer
            for offer in offers
            if offer.url is not None and matches_criteria(offer, self.filters, today=today)
        ]
        candidates.sort(key=self._shortlist_sort_key)
        return {offer.offer_id for offer in candidates[:budget]}

    def _shortlist_sort_key(self, offer: Offer) -> tuple[int, Decimal]:
        breakdown = classify_offer(offer, self.filters["provider_ratings"], self.attractiveness)
        return (_ATTRACTIVENESS_RANK[breakdown.category], self._shortlist_price_key(offer))

    @staticmethod
    def _shortlist_price_key(offer: Offer) -> Decimal:
        # `matches_criteria` already requires a non-None price_per_person.
        assert offer.price_per_person is not None
        return offer.price_per_person

    def _confirm_candidates(
        self, offers: list[Offer], robots_text: str, timeout: float, deadline: float
    ) -> list[Offer]:
        """Confirm at most `max_detail_requests` candidates' real-time price:
        the most attractive offers that could plausibly pass filtering once
        confirmed (see `_shortlist`), never simply the first offers TUI's own
        listing order returns.

        A rejected or inconclusive confirmation never fails the cycle: the
        original, unconfirmed offer is kept with its rejection reason
        recorded.
        """
        budget = self.configuration.get("max_detail_requests", 1)
        shortlisted = self._shortlist(offers)
        logger.info(
            "TUI: %s of %s offers shortlisted for detail confirmation (budget %s)",
            len(shortlisted),
            len(offers),
            budget,
        )
        detail_calls = 0
        deadline_exceeded = False
        result: list[Offer] = []
        for offer in offers:
            if offer.offer_id not in shortlisted or offer.url is None:
                result.append(offer)
                continue
            if not deadline_exceeded and self.clock() >= deadline:
                deadline_exceeded = True
                logger.warning(
                    "TUI detail confirmation stopped (cycle deadline exceeded); "
                    "remaining offer(s) stay unconfirmed"
                )
            if deadline_exceeded:
                result.append(offer)
                continue
            detail_calls += 1
            try:
                detail_path = urlsplit(offer.url).path
                price_delay = robots_policy(robots_text, detail_path)
                if price_delay:
                    self.sleep(price_delay)
                price_body = self.capture_price(offer.url, timeout)
                result.append(confirm_realtime_price(offer, price_body))
            except (ValueError, KeyError, TypeError, IndexError, TuiError) as exc:
                logger.warning("TUI real-time price check rejected for %s: %s", offer.offer_id, exc)
                result.append(replace(offer, price_verification_reason=str(exc)))
        logger.info(
            "TUI: %s detail confirmation attempt(s) out of %s offers",
            detail_calls,
            len(offers),
        )
        return result

    def fetch_watchlist_offers(self, entries: Sequence[HotelWatchlistEntry]) -> list[Offer]:
        """One targeted TUI search for the hotel watchlist, entirely separate
        from `fetch()`'s own standard search (see `tui_query.
        build_watchlist_search_path`'s docstring for why: `fetch()`'s own
        `amountRange` facet is a query-level block on TUI's own search API,
        not merely a client-side filter, so a watched hotel priced above the
        standard 1500 PLN/person cap can never appear in its results at all).

        Candidates are selected by hotel identity only (`watchlist.
        hotel_identity_matches`), never `filtering.matches_criteria`: the
        watchlist's own price/nights/airport rules are checked later, once
        confirmed, by `pipeline.filter_watchlist_batch`. Up to
        `max_detail_requests` matched candidates then get the same real-time
        price confirmation `_confirm_candidates` uses for the standard search,
        so `watchlist.matches()`'s price-completeness gate can accept them (TUI
        is not in `filters["accept_incomplete_price_from"]`). A watched hotel
        with `provider_destinations["tui"]` is searched by that destination (one
        query for all such hotels, typically one page); the rest share the
        unscoped, price-sorted query bounded by `watchlist_max_pages`. A watched hotel
        without any two-adult entry (see `build_watchlist_search_path`) is
        simply not searched for this cycle -- never an error; the shared
        `robots.txt` request still fails closed exactly like `fetch()`, since
        `Scheduler.run_once` isolates this whole method as one unit.
        """
        scoped, unscoped = split_watchlist_entries(entries)
        supported = [*scoped, *unscoped]
        if not supported:
            return []
        cfg = self.configuration
        timeout = cfg.get("timeout_seconds", 20)
        robots_text = self._robots_text(timeout)
        delay = robots_policy(robots_text, PATH)
        if delay:
            self.sleep(delay)
        deadline = self.clock() + cfg.get("cycle_seconds", 90)
        groups = [group for group in (scoped, unscoped) if group]
        logger.info(
            "TUI watchlist search: destination(s) %s for %s hotel(s); %s unscoped hotel(s)",
            sorted({d for e in scoped if (d := e.get("provider_destinations", {}).get("tui"))}),
            len(scoped),
            len(unscoped),
        )
        # Every group always gets its page 1; `watchlist_max_pages` bounds the
        # listing navigations in total, so splitting into groups never raises it
        # (unless there are more groups than pages, each still gets page 1).
        extra_pages = max(cfg.get("watchlist_max_pages", 1) - len(groups), 0)
        offers: list[Offer] = []
        for group in groups:
            body = self.capture(BASE + build_watchlist_search_path(group), timeout)
            offers += parse_search_response(body, self.wall_clock())
            more, used = self._fetch_additional_watchlist_pages(
                group, body, robots_text, timeout, deadline, extra_pages
            )
            offers += more
            extra_pages -= used
        offers = _dedupe_by_offer_id(offers)
        candidates = [
            offer
            for offer in offers
            if offer.url is not None
            and any(hotel_identity_matches(offer, entry) for entry in supported)
        ]
        logger.info(
            "TUI watchlist: %s offer(s) matched by hotel identity out of %s listed",
            len(candidates),
            len(offers),
        )
        return self._confirm_watchlist_candidates(offers, candidates, robots_text, timeout)

    def _fetch_additional_watchlist_pages(
        self,
        entries: Sequence[HotelWatchlistEntry],
        first_page_body: str,
        robots_text: str,
        timeout: float,
        deadline: float,
        extra_pages: int,
    ) -> tuple[list[Offer], int]:
        """Fetch watchlist page 2..min(pagesCount, 1 + extra_pages); returns the
        offers and the number of pages requested.

        Results are price-ascending, 20 per page, so this only widens coverage
        by the next-cheapest offers; it never scans every page. Same failure
        semantics as `_fetch_additional_pages`: a timeout or exceeded cycle
        deadline keeps pages already fetched, anything else fails the fetch.
        """
        pagination = extract_search_response_pagination(first_page_body)
        pages_count = pagination.get("pagesCount")
        total_pages = pages_count if isinstance(pages_count, int) and pages_count > 0 else 1
        offers: list[Offer] = []
        used = 0
        for page in range(2, min(total_pages, 1 + extra_pages) + 1):
            if self.clock() >= deadline:
                logger.warning(
                    "TUI watchlist pagination stopped before page %s (cycle deadline exceeded)",
                    page,
                )
                break
            page_delay = robots_policy(robots_text, PATH)
            if page_delay:
                self.sleep(page_delay)
            used += 1
            try:
                page_body = self.capture(
                    BASE + build_watchlist_search_path(entries, page=page), timeout
                )
                offers += parse_search_response(page_body, self.wall_clock())
            except TuiTimeout as exc:
                logger.warning(
                    "TUI watchlist pagination stopped at page %s (timeout): %s", page, exc
                )
                break
        return offers, used

    def _confirm_watchlist_candidates(
        self,
        offers: list[Offer],
        candidates: list[Offer],
        robots_text: str,
        timeout: float,
    ) -> list[Offer]:
        """Confirm at most `max_detail_requests` identity-matched candidates'
        real-time price; a rejected or inconclusive confirmation never fails
        the fetch, exactly like `_confirm_candidates`."""
        budget = self.configuration.get("max_detail_requests", 1)
        shortlisted = {offer.offer_id for offer in candidates[:budget]}
        result: list[Offer] = []
        for offer in offers:
            if offer.offer_id not in shortlisted or offer.url is None:
                result.append(offer)
                continue
            try:
                detail_path = urlsplit(offer.url).path
                price_delay = robots_policy(robots_text, detail_path)
                if price_delay:
                    self.sleep(price_delay)
                price_body = self.capture_price(offer.url, timeout)
                result.append(confirm_realtime_price(offer, price_body))
            except (ValueError, KeyError, TypeError, IndexError, TuiError) as exc:
                logger.warning(
                    "TUI watchlist real-time price check rejected for %s: %s",
                    offer.offer_id,
                    exc,
                )
                result.append(replace(offer, price_verification_reason=str(exc)))
        return result
