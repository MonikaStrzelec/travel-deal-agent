"""TUI hybrid provider: HTTP robots check, then one passive Playwright capture.

The confirmed production data source is the client-side
`.../api/services/tui-search/api/search/offers` response, observed by passively
listening after opening a `build_search_path` URL in a real browser -- never fetched
directly (see `tui_browser`). The SSR `/wypoczynek/wyniki-wyszukiwania-samolot`
route itself was confirmed to render only a loading skeleton with no offers via
plain HTTP, so that path is not used here. `filtering.matches()` downstream remains
the sole authority for eligibility (stay length, price/person, etc.) regardless of
what upstream returns.

Pagination is confirmed live (2026-09-22 reconnaissance,
`data/tui-production/pagination-recon-20260922T200838Z/`): a query with
`pagination.pagesCount > 1` (174 pages, 3469 results, using only already-
confirmed `build_search_path` parameters with wider values), followed by one
`page=2` navigation, returned `pagination.page == 1` (0-indexed second page,
consistent with the `page` query parameter being 1-indexed) and a completely
disjoint set of 20 `offerCode`s from page 1 -- genuinely new results, not a
repeat. At most `max_pages` (config, default `1`, MVP value `3`) pages are
fetched, bounded by the *live-observed* `pagination.pagesCount` on page 1 --
this provider never fetches more pages than the site itself reports exist, and
never guesses a page's content without requesting it.

After the (possibly multi-page) listing capture, at most `max_detail_requests`
(default 1) offers that already pass every listing-known filter
(`filtering.matches_criteria`; see `_shortlist`) get one additional, passive
detail-page navigation to confirm real-time price/availability (see
`tui_price.confirm_realtime_price`), mirroring ITAKA's bounded detail-
confirmation budget. With only one detail request per cycle to spend, the
shortlist picks the most promising candidate rather than simply the cheapest
one that clears the bar: candidates are ordered by their listing-time
`attractiveness.classify_offer` category (HOT, then GOOD, then MATCH -- the
existing V0 classification, never a new score) and only within the same
category by ascending `price_per_person` (see `_shortlist`). `price_is_complete`
is set to `True` only for a
structurally confirmed charter-flight package tour whose mandatory TFG+TFP
fund matches the officially confirmed rate -- see `tui_price`'s module
docstring. Every other outcome (non-charter, unavailable, ambiguous,
malformed) leaves the offer unconfirmed (`CHARTER_PACKAGE_NOT_CONFIRMED` or the
specific rejection reason) without ever failing the whole cycle.

**Browser-navigation budget per cycle (TUI is heavier than an HTTP-only source
like Wakacje.pl -- every request here is a full Playwright page load, not a
plain HTTP GET):** at most `max_pages` listing navigations (page 1 is always
fetched; `config.py` caps `max_pages` at 3 for this provider) plus at most
`max_detail_requests` detail navigations (capped at 3). For the shipped
config (`max_pages: 3`, `max_detail_requests: 1`) that is at most 4
navigations per cycle. A transient timeout fetching page 2 or 3 keeps every
page already fetched and simply stops further pagination for that cycle
(`TuiTimeout` only); a block, ambiguous response or schema/robots failure on
any page still fails the whole cycle exactly as before -- pagination never
weakens those existing fail-closed checks. `robots.txt` (one plain HTTP
request, not a navigation) and `timeout_seconds` bound each individual
navigation's own duration.

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
"""

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from ..attractiveness import DEFAULT_ATTRACTIVENESS_CONFIG, classify_offer
from ..config_types import AttractivenessConfig, FilterConfig, ProviderConfig
from ..filtering import matches_criteria
from ..models import Offer, utc_now
from .base import Provider
from .http import Transport, UrllibTransport
from .robots import robots_policy
from .tui_browser import capture_offer_price, capture_search_offers
from .tui_data import extract_search_response_pagination, parse_search_response
from .tui_errors import TuiError, TuiTimeout
from .tui_price import confirm_realtime_price
from .tui_query import PATH, build_search_path

logger = logging.getLogger(__name__)
BASE = "https://www.tui.pl"

# Lower rank sorts first: HOT is the most promising, MATCH merely eligible.
# `attractiveness.classify_offer` never returns anything else.
_ATTRACTIVENESS_RANK = {"HOT": 0, "GOOD": 1, "MATCH": 2}

# Re-exported for existing imports/tests (`from .tui import robots_policy`);
# the implementation now lives in .robots, shared with itaka.py and wakacje.py.
__all__ = ["robots_policy"]


def _dedupe_by_offer_id(offers: list[Offer]) -> list[Offer]:
    """Keep the first occurrence of each offer_id across pages.

    Live reconnaissance found zero overlap between pages, but this stays a
    defensive guard against any future overlap (e.g. sorting shifting between
    two page fetches) -- it never merges different offer_ids, so distinct
    dates/variants of the same hotel are never collapsed into one entry.
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

    def fetch(self) -> list[Offer]:
        cfg = self.configuration
        timeout = cfg.get("timeout_seconds", 20)
        deadline = self.clock() + cfg.get("cycle_seconds", 90)
        robots_response = self.transport.get(BASE + "/robots.txt", timeout)
        if robots_response.status != 200:
            raise ValueError(f"TUI robots.txt HTTP {robots_response.status}; no automatic retry")
        delay = robots_policy(robots_response.text, PATH)
        if delay:
            self.sleep(delay)
        body = self.capture(BASE + self.path, timeout)
        offers = parse_search_response(body, self.wall_clock())
        offers += self._fetch_additional_pages(body, robots_response.text, timeout, deadline)
        offers = _dedupe_by_offer_id(offers)
        logger.info(
            "TUI: %s offers parsed from the passively captured search/offers response(s)",
            len(offers),
        )
        return self._confirm_candidates(offers, robots_response.text, timeout, deadline)

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

        First narrowed to offers that already pass every listing-known filter
        (`filtering.matches_criteria`, which never looks at `price_is_complete`
        -- confirming that exact price is the point of the detail request this
        shortlist feeds; an incomplete price never disqualifies a candidate
        here). Reusing `matches_criteria` directly (rather than re-deriving a
        second copy of the same business rules) means a listing offer with an
        already-known disqualifying field (price over budget, wrong airport/
        board/stars/nights) never spends a detail request that could not
        possibly have turned it eligible.

        With only one detail request typically available per cycle, the
        remaining candidates are then ordered by their listing-time
        `attractiveness.classify_offer` category (HOT before GOOD before
        MATCH -- the existing V0 classification computed straight from
        listing data, never a new score, and never `Offer.final_score`), and
        only within the same category by ascending `price_per_person`; equal
        category and price keep their original listing order (`list.sort` is
        stable). This spends the scarce budget confirming the best-looking
        deal, not merely the cheapest one that clears the eligibility bar.
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
        original, unconfirmed offer is kept with its rejection reason recorded.
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
