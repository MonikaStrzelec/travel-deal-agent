"""Manual public listing and detail checks sharing one bounded HTTP budget."""

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from urllib.error import URLError
from urllib.parse import urlsplit

from ..config_types import FilterConfig, ProviderConfig
from ..filtering import matches_criteria
from ..models import Offer
from .base import Provider
from .http import RequestBudget, Transport, UrllibTransport
from .itaka_data import ParsedPage, normalize_page, normalize_rate, parse_page
from .itaka_details import confirm_detail
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
    ) -> None:
        self.configuration = configuration
        # Unlike rainbow/tui/wakacje.pl, `filters` is optional here: this
        # provider's request shape (a fixed /last-minute/ URL) never depends on
        # it. When given, it is used only to shortlist which listing candidate
        # gets the scarce detail-confirmation request (see fetch()); omitting
        # it (e.g. existing callers/tests built before this) simply disables
        # that shortlist preference and preserves the original listing order.
        self.filters = filters
        self.transport = transport or UrllibTransport()
        self.clock = clock
        self.sleep = sleep
        self.today = today

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

    def _detail_shortlist(self, page: ParsedPage) -> list[tuple[dict[str, object], Offer, str]]:
        """Listing candidates worth a scarce detail request, in listing order."""
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
        if self.filters is None:
            return shortlist
        # An offer failing any listing-only hard filter can never alert, whatever
        # its detail page says, so it is dropped rather than deprioritized.
        today = self.today()
        return [item for item in shortlist if matches_criteria(item[1], self.filters, today)]

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


def _path_and_query(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + ("?" + parts.query if parts.query else "")
