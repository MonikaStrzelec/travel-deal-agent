"""Offline tests for TUI listing pagination (confirmed live 2026-09-22,
`data/tui-production/pagination-recon-20260922T200838Z/`). No network,
no Playwright -- capture/capture_price are injected fakes throughout.
"""

import json
import time
from collections.abc import Callable
from datetime import datetime, timezone

import pytest

from travel_deal_agent.config import Settings
from travel_deal_agent.config_types import ProviderConfig
from travel_deal_agent.providers.tui import TuiProvider
from travel_deal_agent.providers.tui_errors import TuiBlocked, TuiStructureError, TuiTimeout
from travel_deal_agent.providers.tui_query import build_search_path
from tui_support import NOT_AVAILABLE_BODY, FakeTransport, raw_offer, robots_response

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
CONFIG: ProviderConfig = {
    "enabled": True,
    "interval_seconds": 3600,
    "max_pages": 3,
    "max_detail_requests": 1,
}


def page_body(page_number: int, pages_count: int, offer_codes: list[str]) -> str:
    """`page` is 0-indexed in the response, matching the confirmed live shape."""
    return json.dumps(
        {
            "pagination": {
                "page": page_number - 1,
                "pageSize": 20,
                "totalResults": pages_count * 20,
                "sorting": "price",
                "pagesCount": pages_count,
            },
            "offers": [raw_offer(code) for code in offer_codes],
            "responseType": "NORMAL",
            "currency": "PLN",
        }
    )


def expected_page_url(settings: Settings, page: int) -> str:
    from travel_deal_agent.providers.tui import BASE

    return BASE + build_search_path(settings.filters, page=page)


class FakeCapture:
    """Returns bodies[i] (or raises it, if an Exception) for the i-th call."""

    def __init__(self, bodies: list[str | Exception]) -> None:
        self.bodies = list(bodies)
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout_seconds: float) -> str:
        self.calls.append((url, timeout_seconds))
        body = self.bodies.pop(0)
        if isinstance(body, Exception):
            raise body
        return body


def make_provider(
    settings: Settings,
    capture: FakeCapture,
    capture_price: FakeCapture | None = None,
    clock: Callable[[], float] = time.monotonic,
    **cfg: object,
) -> TuiProvider:
    configuration: ProviderConfig = {**CONFIG, **cfg}  # type: ignore[typeddict-item]
    return TuiProvider(
        configuration,
        settings.filters,
        FakeTransport([robots_response()]),
        capture,
        capture_price if capture_price is not None else FakeCapture([NOT_AVAILABLE_BODY]),
        sleep=lambda _: None,
        wall_clock=lambda: NOW,
        clock=clock,
    )


# --- pagesCount reported by page 1 controls how many pages get fetched -----------


def test_pages_count_one_fetches_only_page_one(settings: Settings) -> None:
    capture = FakeCapture([page_body(1, pages_count=1, offer_codes=["A1"])])
    offers = make_provider(settings, capture).fetch()
    assert len(capture.calls) == 1
    assert {o.offer_id for o in offers} == {"A1"}


def test_pages_count_three_with_max_pages_three_fetches_all_three(
    settings: Settings,
) -> None:
    capture = FakeCapture(
        [
            page_body(1, pages_count=3, offer_codes=["A1"]),
            page_body(2, pages_count=3, offer_codes=["B1"]),
            page_body(3, pages_count=3, offer_codes=["C1"]),
        ]
    )
    offers = make_provider(settings, capture).fetch()
    assert len(capture.calls) == 3
    assert {o.offer_id for o in offers} == {"A1", "B1", "C1"}
    # page 2/3 URLs carry the confirmed page= parameter.
    assert capture.calls[1][0] == expected_page_url(settings, page=2)
    assert capture.calls[2][0] == expected_page_url(settings, page=3)


def test_pages_count_ten_with_max_pages_three_stops_at_three(settings: Settings) -> None:
    capture = FakeCapture(
        [
            page_body(1, pages_count=10, offer_codes=["A1"]),
            page_body(2, pages_count=10, offer_codes=["B1"]),
            page_body(3, pages_count=10, offer_codes=["C1"]),
        ]
    )
    offers = make_provider(settings, capture).fetch()
    # Never attempts page 4+ even though pagesCount says there are 10.
    assert len(capture.calls) == 3
    assert {o.offer_id for o in offers} == {"A1", "B1", "C1"}


def test_max_pages_one_fetches_only_page_one_even_if_more_exist(
    settings: Settings,
) -> None:
    capture = FakeCapture([page_body(1, pages_count=5, offer_codes=["A1"])])
    offers = make_provider(settings, capture, max_pages=1).fetch()
    assert len(capture.calls) == 1
    assert {o.offer_id for o in offers} == {"A1"}


def test_page_two_offers_are_genuinely_new(settings: Settings) -> None:
    capture = FakeCapture(
        [
            page_body(1, pages_count=2, offer_codes=["A1", "A2"]),
            page_body(2, pages_count=2, offer_codes=["B1", "B2"]),
        ]
    )
    offers = make_provider(settings, capture).fetch()
    assert {o.offer_id for o in offers} == {"A1", "A2", "B1", "B2"}


# --- dedup: same variant never multiplies; different dates/variants survive -----


def test_duplicate_offer_id_across_pages_is_not_multiplied(settings: Settings) -> None:
    capture = FakeCapture(
        [
            page_body(1, pages_count=2, offer_codes=["A1", "A2"]),
            page_body(2, pages_count=2, offer_codes=["A1", "B1"]),  # A1 repeated
        ]
    )
    offers = make_provider(settings, capture).fetch()
    ids = [o.offer_id for o in offers]
    assert ids.count("A1") == 1
    assert set(ids) == {"A1", "A2", "B1"}


def test_different_dated_variants_of_the_same_hotel_both_survive(
    settings: Settings,
) -> None:
    early = raw_offer("SAMEHOTEL-EARLY", hotelCode="SAMEHTL")
    later = raw_offer(
        "SAMEHOTEL-LATER",
        hotelCode="SAMEHTL",
        departureDate="11.12.2026",
        returnDate="17.12.2026",
    )
    page1 = json.dumps(
        {
            "pagination": {
                "page": 0,
                "pageSize": 20,
                "totalResults": 40,
                "sorting": "price",
                "pagesCount": 2,
            },
            "offers": [early],
        }
    )
    page2 = json.dumps(
        {
            "pagination": {
                "page": 1,
                "pageSize": 20,
                "totalResults": 40,
                "sorting": "price",
                "pagesCount": 2,
            },
            "offers": [later],
        }
    )
    capture = FakeCapture([page1, page2])
    offers = make_provider(settings, capture).fetch()
    assert {o.offer_id for o in offers} == {"SAMEHOTEL-EARLY", "SAMEHOTEL-LATER"}


# --- transient timeout on page 2/3 keeps earlier pages; other errors fail closed -


def test_timeout_on_page_two_keeps_page_one(settings: Settings) -> None:
    capture = FakeCapture(
        [
            page_body(1, pages_count=3, offer_codes=["A1"]),
            TuiTimeout("no matching response"),
        ]
    )
    offers = make_provider(settings, capture).fetch()
    assert len(capture.calls) == 2
    assert {o.offer_id for o in offers} == {"A1"}


def test_timeout_on_page_three_keeps_pages_one_and_two(settings: Settings) -> None:
    capture = FakeCapture(
        [
            page_body(1, pages_count=3, offer_codes=["A1"]),
            page_body(2, pages_count=3, offer_codes=["B1"]),
            TuiTimeout("no matching response"),
        ]
    )
    offers = make_provider(settings, capture).fetch()
    assert len(capture.calls) == 3
    assert {o.offer_id for o in offers} == {"A1", "B1"}


@pytest.mark.parametrize("error", [TuiBlocked("blocked"), TuiStructureError("ambiguous")])
def test_block_or_ambiguous_response_on_page_two_fails_the_whole_cycle(
    error: Exception, settings: Settings
) -> None:
    capture = FakeCapture([page_body(1, pages_count=3, offer_codes=["A1"]), error])
    with pytest.raises(type(error)):
        make_provider(settings, capture).fetch()


def test_malformed_page_two_response_fails_the_whole_cycle(settings: Settings) -> None:
    capture = FakeCapture(
        [page_body(1, pages_count=3, offer_codes=["A1"]), "<html>not json</html>"]
    )
    with pytest.raises(ValueError):
        make_provider(settings, capture).fetch()


# --- aggregate cycle deadline: checked before each subsequent navigation --------


def test_deadline_exceeded_after_page_one_stops_page_two(settings: Settings) -> None:
    capture = FakeCapture([page_body(1, pages_count=3, offer_codes=["A1"])])
    ticks = iter([0.0, 100.0])  # 1st: deadline anchor; 2nd: checked before page 2
    offers = make_provider(
        settings,
        capture,
        clock=lambda: next(ticks),
        max_pages=3,
        max_detail_requests=0,
        cycle_seconds=10,
    ).fetch()
    # page 2 is never attempted; page 1's offer is kept.
    assert len(capture.calls) == 1
    assert {o.offer_id for o in offers} == {"A1"}


def test_deadline_exceeded_after_page_two_stops_page_three(settings: Settings) -> None:
    capture = FakeCapture(
        [
            page_body(1, pages_count=3, offer_codes=["A1"]),
            page_body(2, pages_count=3, offer_codes=["B1"]),
        ]
    )
    ticks = iter([0.0, 5.0, 100.0])  # anchor; before page 2 (ok); before page 3 (exceeded)
    offers = make_provider(
        settings,
        capture,
        clock=lambda: next(ticks),
        max_pages=3,
        max_detail_requests=0,
        cycle_seconds=10,
    ).fetch()
    # page 3 is never attempted; pages 1 and 2's offers are kept.
    assert len(capture.calls) == 2
    assert {o.offer_id for o in offers} == {"A1", "B1"}


def test_deadline_exceeded_before_detail_leaves_offer_incomplete(settings: Settings) -> None:
    capture = FakeCapture([page_body(1, pages_count=1, offer_codes=["A1"])])
    capture_price = FakeCapture([NOT_AVAILABLE_BODY])
    ticks = iter([0.0, 100.0])  # anchor; checked before the one detail navigation
    offers = make_provider(
        settings,
        capture,
        capture_price,
        clock=lambda: next(ticks),
        max_detail_requests=1,
        cycle_seconds=10,
    ).fetch()
    # No detail navigation is attempted; the listing offer stays incomplete.
    assert capture_price.calls == []
    assert len(offers) == 1
    assert offers[0].price_is_complete is False


def test_full_cycle_completes_within_cycle_seconds(settings: Settings) -> None:
    capture = FakeCapture(
        [
            page_body(1, pages_count=3, offer_codes=["A1"]),
            page_body(2, pages_count=3, offer_codes=["B1"]),
            page_body(3, pages_count=3, offer_codes=["C1"]),
        ]
    )
    capture_price = FakeCapture([NOT_AVAILABLE_BODY])
    ticks = iter([0.0, 2.0, 4.0, 6.0])  # every check comfortably before the deadline
    offers = make_provider(
        settings,
        capture,
        capture_price,
        clock=lambda: next(ticks),
        max_pages=3,
        max_detail_requests=1,
        cycle_seconds=90,
    ).fetch()
    assert len(capture.calls) == 3
    assert len(capture_price.calls) == 1
    assert {o.offer_id for o in offers} == {"A1", "B1", "C1"}


def test_missing_pagination_is_treated_as_a_single_page(settings: Settings) -> None:
    # No `pagination` object at all -- never guessed as "many pages".
    capture = FakeCapture([json.dumps({"offers": [raw_offer("A1")]})])
    offers = make_provider(settings, capture).fetch()
    assert len(capture.calls) == 1
    assert {o.offer_id for o in offers} == {"A1"}


# --- browser-navigation budget: listing pages + detail requests, combined -------


def test_navigation_budget_is_listing_pages_plus_detail_requests(
    settings: Settings,
) -> None:
    capture = FakeCapture(
        [
            page_body(1, pages_count=3, offer_codes=["A1"]),
            page_body(2, pages_count=3, offer_codes=["B1"]),
            page_body(3, pages_count=3, offer_codes=["C1"]),
        ]
    )
    capture_price = FakeCapture([NOT_AVAILABLE_BODY])
    make_provider(settings, capture, capture_price, max_pages=3, max_detail_requests=1).fetch()
    total_navigations = len(capture.calls) + len(capture_price.calls)
    assert len(capture.calls) == 3
    assert len(capture_price.calls) == 1
    assert (
        total_navigations == 4
    )  # mirrors the local CONFIG budget above (max_pages=3, max_detail_requests=1)


# --- rating: disabled threshold never blocks; rating/reviews still flow through --


def test_disabled_rating_threshold_does_not_block_pagination_offers(
    settings: Settings,
) -> None:
    # provider_ratings["tui"] defaults to disabled (config.py); a real
    # TripAdvisor rating must still reach the Offer and never be treated as a
    # hard filter regardless of how many pages it came from.
    assert settings.filters["provider_ratings"]["tui"]["enabled"] is False
    capture = FakeCapture([page_body(1, pages_count=1, offer_codes=["A1"])])
    offers = make_provider(settings, capture).fetch()
    assert offers[0].rating == 4.3
    assert offers[0].number_of_reviews == 777
    assert offers[0].provider_rating_max == 5.0
