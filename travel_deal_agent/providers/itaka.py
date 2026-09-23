"""Manual public listing and detail checks sharing one bounded HTTP budget."""

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from urllib.parse import urlsplit

from ..config_types import ProviderConfig
from ..models import Offer
from .base import Provider
from .http import RequestBudget, Transport, UrllibTransport
from .itaka_data import normalize_page, normalize_rate, parse_page
from .itaka_details import confirm_detail
from .robots import robots_policy

logger = logging.getLogger(__name__)
BASE = "https://www.itaka.pl"

# Re-exported for existing imports/tests (`from .itaka import robots_policy`);
# the implementation now lives in .robots, shared with tui.py and wakacje.py.
__all__ = ["robots_policy"]


class ItakaProvider(Provider):
    name = "itaka"

    def __init__(
        self,
        configuration: ProviderConfig,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.configuration = configuration
        self.transport = transport or UrllibTransport()
        self.clock = clock
        self.sleep = sleep

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
            parts = urlsplit(url)
            robots_policy(robots, parts.path + ("?" + parts.query if parts.query else ""))
            page = parse_page(budget.get(url).text)
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
            for raw in page.rates:
                if (
                    detail_calls >= cfg.get("max_detail_requests", 1)
                    or budget.calls >= budget.max_requests
                ):
                    break
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
                robots_policy(
                    robots, detail_url.path + ("?" + detail_url.query if detail_url.query else "")
                )
                response = budget.get(candidate.url)
                detail_calls += 1
                try:
                    offers[candidate.offer_id] = confirm_detail(candidate, raw, response.text)
                except (ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
                    logger.warning("ITAKA detail rejected for %s: %s", candidate.offer_id, exc)
                    offers[candidate.offer_id] = replace(
                        candidate, price_verification_reason=str(exc)
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
