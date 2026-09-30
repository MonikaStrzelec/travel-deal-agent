"""TUI watchlist pagination (`watchlist_max_pages`) and the shared robots.txt cache.

Injected capture/transport/clock fakes; no network, no Playwright.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest
from test_tui_watchlist import FakeCapture, charter_realtime_body, watchlist_entry

from conftest import WriteConfig
from travel_deal_agent.config import Settings, load_settings
from travel_deal_agent.config_types import ProviderConfig
from travel_deal_agent.providers.http import Response
from travel_deal_agent.providers.tui import BASE, ROBOTS_CACHE_TTL_SECONDS, TuiProvider
from travel_deal_agent.providers.tui_errors import TuiTimeout
from travel_deal_agent.providers.tui_query import build_watchlist_search_path
from tui_support import NOT_AVAILABLE_BODY, FakeTransport, raw_offer, robots_response

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)


class PagedCapture:
    """Returns bodies[i] (raising it if an Exception) for the i-th call."""

    def __init__(self, bodies: list[str | Exception]) -> None:
        self.bodies = list(bodies)
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout_seconds: float) -> str:
        self.calls.append((url, timeout_seconds))
        body = self.bodies.pop(0)
        if isinstance(body, Exception):
            raise body
        return body


def watch_raw(code: str) -> dict[str, object]:
    return raw_offer(
        code,
        hotelName="Test Watchlist Hotel" if code.startswith("W") else "Other Hotel",
        discountPerPersonPrice="2200",
        originalPerPersonPrice="2200",
        discountFullPrice="4400",
        originalFullPrice="4400",
    )


def page_body(page_number: int, pages_count: int, codes: list[str]) -> str:
    return json.dumps(
        {
            "pagination": {
                "page": page_number - 1,
                "pageSize": 20,
                "totalResults": pages_count * 20,
                "sorting": "price",
                "pagesCount": pages_count,
            },
            "offers": [watch_raw(code) for code in codes],
        }
    )


def make_provider(
    settings: Settings,
    capture: PagedCapture,
    transport: FakeTransport | None = None,
    **cfg: object,
) -> TuiProvider:
    base: dict[str, object] = {
        "enabled": True,
        "interval_seconds": 3600,
        "max_detail_requests": 0,
        **cfg,
    }
    configuration = cast(ProviderConfig, base)
    return TuiProvider(
        configuration,
        settings.filters,
        transport or FakeTransport([robots_response()]),
        capture,
        FakeCapture(NOT_AVAILABLE_BODY),
        sleep=lambda _: None,
        wall_clock=lambda: NOW,
        clock=lambda: 0.0,
    )


def urls_of(capture: PagedCapture) -> list[str]:
    return [url for url, _ in capture.calls]


def test_pages_one_to_limit_are_fetched_and_never_beyond(settings: Settings) -> None:
    capture = PagedCapture(
        [page_body(1, 33, ["A1"]), page_body(2, 33, ["A2"]), page_body(3, 33, ["A3"])]
    )
    p = make_provider(settings, capture, watchlist_max_pages=2)

    offers = p.fetch_watchlist_offers([watchlist_entry()])

    entries = [watchlist_entry()]
    assert urls_of(capture) == [
        BASE + build_watchlist_search_path(entries, page=1),
        BASE + build_watchlist_search_path(entries, page=2),
    ]
    assert [o.offer_id for o in offers] == ["A1", "A2"]


def test_default_is_a_single_page(settings: Settings) -> None:
    capture = PagedCapture([page_body(1, 33, ["A1"]), page_body(2, 33, ["A2"])])
    p = make_provider(settings, capture)

    p.fetch_watchlist_offers([watchlist_entry()])

    assert len(capture.calls) == 1


def test_pages_count_below_limit_stops_early(settings: Settings) -> None:
    capture = PagedCapture([page_body(1, 2, ["A1"]), page_body(2, 2, ["A2"])])
    p = make_provider(settings, capture, watchlist_max_pages=3)

    p.fetch_watchlist_offers([watchlist_entry()])

    assert len(capture.calls) == 2


def test_offer_repeated_across_pages_is_deduplicated(settings: Settings) -> None:
    capture = PagedCapture([page_body(1, 5, ["W1", "A1"]), page_body(2, 5, ["W1", "A2"])])
    p = make_provider(settings, capture, watchlist_max_pages=2)

    offers = p.fetch_watchlist_offers([watchlist_entry()])

    assert [o.offer_id for o in offers] == ["W1", "A1", "A2"]


def test_hotel_found_only_on_later_page_is_confirmed(settings: Settings) -> None:
    capture = PagedCapture([page_body(1, 5, ["A1"]), page_body(2, 5, ["W9"])])
    price_capture = FakeCapture(charter_realtime_body(watch_raw("W9")))
    p = make_provider(settings, capture, watchlist_max_pages=2, max_detail_requests=1)
    p.capture_price = price_capture

    offers = p.fetch_watchlist_offers([watchlist_entry()])

    confirmed = [o for o in offers if o.price_is_complete]
    assert [o.offer_id for o in confirmed] == ["W9"]
    assert len(price_capture.calls) == 1  # detail budget unchanged by pagination


def test_every_page_keeps_the_watchlist_price_cap_not_the_global_one(settings: Settings) -> None:
    capture = PagedCapture([page_body(1, 5, ["A1"]), page_body(2, 5, ["A2"])])
    p = make_provider(settings, capture, watchlist_max_pages=2)

    p.fetch_watchlist_offers([watchlist_entry()])

    for url in urls_of(capture):
        q = parse_qs(urlsplit(url).query)["q"][0]
        assert ":amountRange:#4600:" in q


def test_timeout_on_later_page_keeps_earlier_pages(settings: Settings) -> None:
    capture = PagedCapture([page_body(1, 5, ["A1"]), TuiTimeout("slow")])
    p = make_provider(settings, capture, watchlist_max_pages=3)

    offers = p.fetch_watchlist_offers([watchlist_entry()])

    assert [o.offer_id for o in offers] == ["A1"]
    assert len(capture.calls) == 2


def test_standard_fetch_is_unaffected_by_watchlist_max_pages(settings: Settings) -> None:
    capture = PagedCapture([page_body(1, 33, ["A1"])])
    p = make_provider(settings, capture, watchlist_max_pages=3, max_pages=1)

    p.fetch()

    assert len(capture.calls) == 1


# --- shared robots.txt cache -------------------------------------------------------


def test_standard_and_watchlist_fetch_share_one_robots_request(settings: Settings) -> None:
    transport = FakeTransport([robots_response()])
    capture = PagedCapture([page_body(1, 1, ["A1"]), page_body(1, 1, ["A2"])])
    p = make_provider(settings, capture, transport, max_pages=1)

    p.fetch()
    p.fetch_watchlist_offers([watchlist_entry()])

    assert transport.urls == [BASE + "/robots.txt"]


def test_robots_is_refetched_after_the_ttl(settings: Settings) -> None:
    transport = FakeTransport([robots_response(), robots_response()])
    capture = PagedCapture([page_body(1, 1, ["A1"]), page_body(1, 1, ["A2"])])
    now = [NOW]
    p = make_provider(settings, capture, transport, max_pages=1)
    p.wall_clock = lambda: now[0]

    p.fetch()
    now[0] = NOW + timedelta(seconds=ROBOTS_CACHE_TTL_SECONDS + 1)
    p.fetch_watchlist_offers([watchlist_entry()])

    assert transport.urls == [BASE + "/robots.txt"] * 2


def test_failed_robots_response_is_not_cached(settings: Settings) -> None:
    transport = FakeTransport([Response(503, "", {}), robots_response()])
    capture = PagedCapture([page_body(1, 1, ["A1"])])
    p = make_provider(settings, capture, transport, max_pages=1)

    with pytest.raises(ValueError, match="robots.txt HTTP 503"):
        p.fetch()
    p.fetch()

    assert len(transport.urls) == 2


# --- config ----------------------------------------------------------------------


def test_config_parses_watchlist_max_pages(write_config: WriteConfig) -> None:
    write_config(lambda raw: raw["providers"]["tui"].update(watchlist_max_pages=2))

    assert load_settings().providers["tui"]["watchlist_max_pages"] == 2


@pytest.mark.parametrize("value", [0, 4])
def test_watchlist_max_pages_is_bounded(write_config: WriteConfig, value: int) -> None:
    write_config(lambda raw: raw["providers"]["tui"].update(watchlist_max_pages=value))

    with pytest.raises(ValueError, match="watchlist_max_pages must be between 1 and 3"):
        load_settings()
