"""Manual public listing and detail checks sharing one bounded HTTP budget."""

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date
from urllib.error import URLError
from urllib.parse import urlsplit

from ..config_types import FilterConfig, HotelWatchlistEntry, ProviderConfig
from ..filtering import matches_criteria
from ..models import Offer
from ..watchlist import hotel_identity_matches
from .base import Provider
from .http import RequestBudget, Transport, UrllibTransport
from .itaka_data import ParsedPage, normalize_page, normalize_rate, parse_page
from .itaka_details import confirm_detail
from .itaka_query import build_watchlist_path, destination_groups
from .robots import robots_policy

logger = logging.getLogger(__name__)
BASE = "https://www.itaka.pl"


class ItakaProvider(Provider):
    name = "itaka"

    def __init__(
        self,
        configuration: ProviderConfig,
        filters: FilterConfig | None = None,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = date.today,
        hotel_watchlist: Sequence[HotelWatchlistEntry] = (),
    ) -> None:
        self.configuration = configuration
        # Unlike rainbow/tui/wakacje.pl, `filters` is optional here: this
        # provider's request shape (a fixed /last-minute/ URL) never depends on
        # it. When given, it is used only to shortlist which listing candidate
        # gets the scarce detail-confirmation request (see fetch()); omitting
        # it disables that preference and keeps the original listing order.
        self.filters = filters
        self.transport = transport or UrllibTransport()
        self.clock = clock
        self.sleep = sleep
        self.today = today
        # Widens `_detail_shortlist` below with watched-hotel candidates (see
        # its docstring), request-budget-neutral. Hotels with a configured
        # destination slug are also searched directly by `fetch_watchlist_offers`.
        self.hotel_watchlist = hotel_watchlist

    def fetch(self) -> list[Offer]:
        cfg = self.configuration
        budget = RequestBudget(
            self.transport,
            cfg.get("max_requests", 3),
            cfg.get("timeout_seconds", 15),
            cfg.get("cycle_seconds", 60),
            cfg.get("request_gap_seconds", 5),
            self.clock,
            self.sleep,
        )
        robots = budget.get(BASE + "/robots.txt").text
        budget.gap = max(budget.gap, robots_policy(robots, "/last-minute/"))
        maximum = cfg.get("max_pages", 2)
        page_number, expected_skip = 1, 0
        previous_take: int | None = None
        previous_count: int | None = None
        detail_calls = 0
        offers: dict[str, Offer] = {}
        while maximum is None or page_number <= maximum:
            if budget.calls >= budget.max_requests:
                logger.warning("ITAKA partial coverage: request budget reached")
                break
            url = BASE + "/last-minute/" + (f"?page={page_number}" if page_number > 1 else "")
            robots_policy(robots, _path_and_query(url))
            try:
                body = budget.get(url).text
            except (TimeoutError, URLError) as exc:
                # Matches wakacje.py's documented policy exactly: only a transient
                # network error (never a policy/block status, which stays a
                # fail-closed ValueError from RequestBudget.get()) stops further
                # pagination while keeping every offer already parsed so far.
                logger.warning(
                    "ITAKA partial coverage: listing page %s skipped after a transient "
                    "network error (%s); no retry, stopping pagination",
                    page_number,
                    exc,
                )
                break
            page = parse_page(body)
            if page.skip != expected_skip or (
                previous_take is not None and page.take != previous_take
            ):
                raise ValueError("ITAKA pagination did not advance as expected")
            if previous_count is not None and page.count != previous_count:
                raise ValueError("ITAKA listing count changed during pagination")
            previous_count = page.count
            batch = normalize_page(page)
            if len({o.offer_id for o in batch}) != len(batch) or any(
                offer.offer_id in offers for offer in batch
            ):
                raise ValueError("ITAKA repeated variants across pages")
            offers.update((offer.offer_id, offer) for offer in batch)
            detail_calls = self._confirm_details(
                budget, robots, self._detail_shortlist(page), offers, detail_calls
            )
            if page.skip + len(page.rates) >= page.count:
                break
            if len(page.rates) != page.take:
                raise ValueError("ITAKA incomplete page before end of listing")
            expected_skip += page.take
            previous_take = page.take
            page_number += 1
        else:
            logger.warning("ITAKA partial coverage: page limit reached")
        logger.info(
            "ITAKA: %s offers; %s confirmed booking prices; %s detail requests",
            len(offers),
            sum(o.price_is_complete for o in offers.values()),
            detail_calls,
        )
        return list(offers.values())

    def fetch_watchlist_offers(self, entries: Sequence[HotelWatchlistEntry]) -> list[Offer]:
        """Destination-scoped, cheapest-first search for watched hotels with a
        configured `provider_destinations["itaka"]` country slug (see
        `itaka_query`), entirely separate from `fetch()`'s `/last-minute/` pages.

        One query per distinct slug (hotels sharing a slug share it), at most
        `max_pages` pages each, price-ascending and capped by the widest
        per-person watchlist limit in the group -- not by `filters["max_price"]`.
        The request budget is the standard cycle's `max_requests`, with one
        request always reserved for the detail confirmation of an
        identity-matched candidate (`max_detail_requests`), so this never asks
        more of ITAKA than a normal cycle may. Candidates are chosen by hotel
        identity only; each entry's own price/nights/airport rules are checked
        later by `pipeline.filter_watchlist_batch`. A hotel without a slug is
        not searched here (it still benefits from `_detail_shortlist`).
        """
        groups = destination_groups(entries)
        if not groups:
            return []
        cfg = self.configuration
        max_detail = cfg.get("max_detail_requests", 1)
        budget = RequestBudget(
            self.transport,
            cfg.get("max_requests", 3),
            cfg.get("timeout_seconds", 15),
            cfg.get("cycle_seconds", 60),
            cfg.get("request_gap_seconds", 5),
            self.clock,
            self.sleep,
        )
        robots = budget.get(BASE + "/robots.txt").text
        reserved = 1 if max_detail > 0 else 0
        maximum = cfg.get("max_pages", 2)
        offers: dict[str, Offer] = {}
        candidates: list[tuple[dict[str, object], Offer, str]] = []
        for slug, group in groups.items():
            logger.info("ITAKA watchlist search: destination %r for %s hotel(s)", slug, len(group))
            expected_skip, previous_take, page_number = 0, None, 1
            while maximum is None or page_number <= maximum:
                if budget.calls >= budget.max_requests - reserved:
                    logger.warning("ITAKA watchlist partial coverage: request budget reached")
                    break
                path = build_watchlist_path(slug, group, page=page_number)
                budget.gap = max(budget.gap, robots_policy(robots, path))
                try:
                    body = budget.get(BASE + path).text
                except (TimeoutError, URLError) as exc:
                    logger.warning(
                        "ITAKA watchlist page %s for %r skipped after a transient network "
                        "error (%s); no retry",
                        page_number,
                        slug,
                        exc,
                    )
                    break
                page = parse_page(body)
                if page.skip != expected_skip or (
                    previous_take is not None and page.take != previous_take
                ):
                    raise ValueError("ITAKA watchlist pagination did not advance as expected")
                for offer in normalize_page(page):
                    offers.setdefault(offer.offer_id, offer)
                candidates += [
                    item
                    for item in _listing_candidates(page)
                    if any(hotel_identity_matches(item[1], entry) for entry in group)
                ]
                if page.skip + len(page.rates) >= page.count:
                    break
                if len(page.rates) != page.take:
                    raise ValueError("ITAKA incomplete watchlist page before end of listing")
                expected_skip += page.take
                previous_take = page.take
                page_number += 1
        shortlist = list({item[1].offer_id: item for item in reversed(candidates)}.values())[::-1]
        self._confirm_details(budget, robots, shortlist, offers, 0)
        logger.info(
            "ITAKA watchlist: %s offer(s), %s matched by hotel identity, %s request(s)",
            len(offers),
            len(shortlist),
            budget.calls,
        )
        return list(offers.values())

    def _detail_shortlist(self, page: ParsedPage) -> list[tuple[dict[str, object], Offer, str]]:
        """Listing candidates worth a scarce detail request, in listing order.

        Two independent reasons a candidate earns a spot here, tried in this
        order: (1) the hotel watchlist -- identity only (`watchlist.
        hotel_identity_matches`), regardless of price/board/stars, since a
        watched hotel is never subject to those (see
        `config_types.HotelWatchlistEntry`) and its own eligibility (price
        ceiling, nights, airport) is checked later, once confirmed, by
        `pipeline.filter_watchlist_batch`; (2) the standard search's own
        `matches_criteria` (price, stars, board, ...), exactly as before.

        Watchlist candidates are listed first so they win the scarce
        `max_detail_requests` budget over an otherwise-eligible standard
        candidate (see `_confirm_details`) -- this never spends an extra
        request, it only changes which candidate the existing budget goes to.
        Without this, a watched hotel priced above `filters["max_price"]`
        would never pass the standard-search-only shortlist below, so its
        price would never be confirmed and `watchlist.matches()` would always
        reject it as incomplete (see `itaka.py`'s module-level rationale for
        why ITAKA, unlike wakacje.pl, has no separate targeted per-hotel fetch).
        """
        shortlist = _listing_candidates(page)
        watchlisted = [
            item
            for item in shortlist
            if any(hotel_identity_matches(item[1], entry) for entry in self.hotel_watchlist)
        ]
        if self.filters is None:
            standard = shortlist
        else:
            # An offer failing any listing-only hard filter can never alert
            # through the standard search, whatever its detail page says, so
            # it is dropped rather than deprioritized.
            today = self.today()
            standard = [
                item for item in shortlist if matches_criteria(item[1], self.filters, today)
            ]
        watchlisted_ids = {item[1].offer_id for item in watchlisted}
        return watchlisted + [item for item in standard if item[1].offer_id not in watchlisted_ids]

    def _confirm_details(
        self,
        budget: RequestBudget,
        robots: str,
        shortlist: list[tuple[dict[str, object], Offer, str]],
        offers: dict[str, Offer],
        detail_calls: int,
    ) -> int:
        """Confirm shortlisted offers in place; return the updated detail request count."""
        for raw, candidate, url in shortlist:
            if (
                detail_calls >= self.configuration.get("max_detail_requests", 1)
                or budget.calls >= budget.max_requests
            ):
                break
            robots_policy(robots, _path_and_query(url))
            try:
                response = budget.get(url)
            except (TimeoutError, URLError) as exc:
                # A transient network error skips only this candidate; it keeps
                # its listing-only, price_is_complete=False data.
                logger.warning(
                    "ITAKA detail request skipped for %s after a transient network "
                    "error (%s); no retry",
                    candidate.offer_id,
                    exc,
                )
                continue
            detail_calls += 1
            try:
                offers[candidate.offer_id] = confirm_detail(candidate, raw, response.text)
            except (ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
                logger.warning("ITAKA detail rejected for %s: %s", candidate.offer_id, exc)
                offers[candidate.offer_id] = replace(candidate, price_verification_reason=str(exc))
        return detail_calls


def _listing_candidates(page: ParsedPage) -> list[tuple[dict[str, object], Offer, str]]:
    """Every rate on the page that normalizes and has a confirmable detail URL."""
    shortlist: list[tuple[dict[str, object], Offer, str]] = []
    for raw in page.rates:
        try:
            candidate = normalize_rate(raw, page.links)
        except (ValueError, KeyError, TypeError):
            continue
        if candidate.url is None:
            continue
        detail_url = urlsplit(candidate.url)
        if (
            detail_url.scheme != "https"
            or detail_url.netloc != "www.itaka.pl"
            or not detail_url.path.startswith("/wczasy/")
        ):
            continue
        shortlist.append((raw, candidate, candidate.url))
    return shortlist


def _path_and_query(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + ("?" + parts.query if parts.query else "")
