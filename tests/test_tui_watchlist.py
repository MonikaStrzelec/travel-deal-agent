"""TUI's targeted hotel-watchlist fetch: entirely separate from `fetch()`'s
own standard search query (see `tui.py`'s module docstring -- `fetch()`'s own
`amountRange` facet is a query-level block on TUI's own search API, so a
watched hotel priced above the standard 1500 PLN/person cap can never appear
in its results at all).

Injected capture/capture_price fakes throughout; no network, no Playwright.
"""

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import cast

from conftest import WriteHotelWatchlist
from travel_deal_agent.config import Settings, load_settings
from travel_deal_agent.config_types import HotelWatchlistEntry, ProviderConfig
from travel_deal_agent.models import Offer
from travel_deal_agent.notifications import Notifier
from travel_deal_agent.providers.tui import BASE, TuiProvider
from travel_deal_agent.providers.tui_errors import TuiTimeout
from travel_deal_agent.providers.tui_query import build_watchlist_search_path
from travel_deal_agent.scheduler import Scheduler
from travel_deal_agent.storage import Notification, Store
from tui_support import NOT_AVAILABLE_BODY, FakeTransport, raw_offer, robots_response

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
CONFIG: ProviderConfig = {"enabled": True, "interval_seconds": 3600}


def watchlist_entry(**overrides: object) -> HotelWatchlistEntry:
    base: dict[str, object] = {
        "name": "Test Watchlist Hotel",
        "aliases": [],
        "people": 2,
        "max_price_per_person": "2300",
        "min_nights": None,
        "airports": ["LCJ", "WAW", "WMI", "KTW", "WRO"],
    }
    base.update(overrides)
    return cast(HotelWatchlistEntry, base)


def watchlisted_raw_offer(**overrides: object) -> dict[str, object]:
    base = raw_offer(
        "WATCH0001",
        hotelName="Test Watchlist Hotel",
        discountPerPersonPrice="2200",
        originalPerPersonPrice="2200",
        discountFullPrice="4400",
        originalFullPrice="4400",
    )
    base.update(overrides)
    return base


class FakeCapture:
    def __init__(self, body: str | Exception) -> None:
        self.body = body
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout_seconds: float) -> str:
        self.calls.append((url, timeout_seconds))
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def provider(
    settings: Settings,
    transport: FakeTransport,
    capture: FakeCapture,
    capture_price: FakeCapture | None = None,
    **cfg: object,
) -> TuiProvider:
    configuration: ProviderConfig = {**CONFIG, **cfg}  # type: ignore[typeddict-item]
    return TuiProvider(
        configuration,
        settings.filters,
        transport,
        capture,
        capture_price if capture_price is not None else FakeCapture(NOT_AVAILABLE_BODY),
        sleep=lambda _: None,
        wall_clock=lambda: NOW,
    )


def charter_realtime_body(raw: dict[str, object], **overrides: object) -> str:
    body: dict[str, object] = {
        "offerStatus": "AVAILABLE",
        "offerCode": raw["offerCode"],
        "priceDetails": {
            "totalPrice": int(str(raw["discountFullPrice"])),
            "totalDiscountPrice": int(str(raw["discountFullPrice"])),
            "priceGuaranteeFund": 60,
            "priceGuaranteeFundInfo": "Turystyczny Fundusz Gwarancyjny",
            "priceDifference": 0,
            "currency": "PLN",
            "pricePerPerson": int(str(raw["discountPerPersonPrice"])),
            "factor": 1.0,
            "discountPercentage": 0,
        },
        "travellerCount": {"adults": 2, "children": 0},
        "outboundFlight": {"departureAirportCode": "KTW"},
        "accommodations": [{"hotelCode": raw["hotelCode"], "duration": raw["duration"]}],
        "tags": ["CHARTER_FLIGHT"],
        "offerTravelType": "BYPLANE",
        "analyticsData": {"values": {"flight_type": "CHART"}},
    }
    body.update(overrides)
    return json.dumps(body)


# --- Basic mechanics: one targeted request, identity-based candidate selection --


def test_fetch_watchlist_offers_requests_the_widened_query(settings: Settings) -> None:
    raw = watchlisted_raw_offer()
    transport = FakeTransport([robots_response()])
    capture = FakeCapture(json.dumps({"offers": [raw]}))
    p = provider(settings, transport, capture)

    offers = p.fetch_watchlist_offers([watchlist_entry()])

    assert len(offers) == 1
    assert transport.urls == [BASE + "/robots.txt"]
    expected_path = build_watchlist_search_path([watchlist_entry()])
    assert capture.calls == [(BASE + expected_path, 20)]


def test_no_two_adult_entries_means_no_request_at_all(settings: Settings) -> None:
    transport = FakeTransport([])
    capture = FakeCapture(json.dumps({"offers": []}))
    p = provider(settings, transport, capture)

    offers = p.fetch_watchlist_offers([watchlist_entry(people=3)])

    assert offers == []
    assert transport.urls == []
    assert capture.calls == []


def test_empty_watchlist_means_no_request_at_all(settings: Settings) -> None:
    transport = FakeTransport([])
    capture = FakeCapture(json.dumps({"offers": []}))
    p = provider(settings, transport, capture)

    offers = p.fetch_watchlist_offers([])

    assert offers == []
    assert transport.urls == []
    assert capture.calls == []


def test_candidates_are_selected_by_hotel_identity_not_price_or_board(
    settings: Settings,
) -> None:
    # Arrange: the watched hotel (above the standard price cap, and priced/
    # boarded in a way the standard search would never allow) alongside an
    # unrelated, cheap hotel. Only the identity match should be confirmed.
    matching = watchlisted_raw_offer()
    unrelated = raw_offer("OTHER0001", hotelName="Some Other Hotel")
    transport = FakeTransport([robots_response()])
    capture = FakeCapture(json.dumps({"offers": [unrelated, matching]}))
    capture_price = FakeCapture(charter_realtime_body(matching))
    p = provider(settings, transport, capture, capture_price)

    offers = p.fetch_watchlist_offers([watchlist_entry()])

    assert len(offers) == 2
    assert len(capture_price.calls) == 1
    assert capture_price.calls[0][0] == next(
        o.url for o in offers if o.hotel_name == matching["hotelName"]
    )


def test_confirmation_budget_reuses_max_detail_requests(settings: Settings) -> None:
    first = watchlisted_raw_offer(offerCode="WATCH0001", hotelCode="WATCH000")
    second = watchlisted_raw_offer(
        offerCode="WATCH0002",
        hotelCode="WATCH001",
        hotelName="Second Watchlist Hotel",
        offerUrl="/wypoczynek/egipt/second-watchlist-hotel/OfferCodeWS/WATCH0002",
    )
    entries = [watchlist_entry(), watchlist_entry(name="Second Watchlist Hotel")]
    transport = FakeTransport([robots_response()])
    capture = FakeCapture(json.dumps({"offers": [first, second]}))
    capture_price = FakeCapture(NOT_AVAILABLE_BODY)
    p = provider(settings, transport, capture, capture_price, max_detail_requests=1)

    offers = p.fetch_watchlist_offers(entries)

    assert len(offers) == 2
    assert len(capture_price.calls) == 1


def test_confirmed_charter_offer_becomes_price_complete(settings: Settings) -> None:
    raw = watchlisted_raw_offer()
    transport = FakeTransport([robots_response()])
    capture = FakeCapture(json.dumps({"offers": [raw]}))
    capture_price = FakeCapture(charter_realtime_body(raw))
    p = provider(settings, transport, capture, capture_price)

    offers = p.fetch_watchlist_offers([watchlist_entry()])

    assert len(offers) == 1
    assert offers[0].price_is_complete is True
    assert offers[0].price_per_person == Decimal("2230")  # 4400 + 60 TFG/TFP = 4460 / 2


def test_confirmation_failure_is_isolated_offer_stays_unconfirmed(
    settings: Settings,
) -> None:
    raw = watchlisted_raw_offer()
    transport = FakeTransport([robots_response()])
    capture = FakeCapture(json.dumps({"offers": [raw]}))
    capture_price = FakeCapture(TuiTimeout("no matching response"))
    p = provider(settings, transport, capture, capture_price)

    offers = p.fetch_watchlist_offers([watchlist_entry()])

    assert len(offers) == 1
    assert offers[0].price_is_complete is False


# --- End-to-end: Scheduler wiring, price band, dedup, failure isolation -----


class _RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> None:
        self.sent.append(notification)


class _TuiWatchlistOnlyStub(TuiProvider):
    """A TuiProvider whose standard fetch() never touches the network -- only
    `fetch_watchlist_offers()` (still capture-backed) is under test."""

    def fetch(self) -> list[Offer]:
        return []


def _tui_settings(write_hotel_watchlist: WriteHotelWatchlist) -> Settings:
    write_hotel_watchlist([dict(watchlist_entry())])
    settings = load_settings()
    return _enable_only_tui(settings)


def test_targeted_offer_priced_above_1500_and_at_most_2300_is_accepted(
    store: Store, write_hotel_watchlist: WriteHotelWatchlist
) -> None:
    settings = _tui_settings(write_hotel_watchlist)
    raw = watchlisted_raw_offer()
    transport = FakeTransport([robots_response()])
    capture = FakeCapture(json.dumps({"offers": [raw]}))
    capture_price = FakeCapture(charter_realtime_body(raw))
    p = _TuiWatchlistOnlyStub(
        settings.providers["tui"],
        settings.filters,
        transport,
        capture,
        capture_price,
        sleep=lambda _: None,
        wall_clock=lambda: NOW,
    )
    notifier = _RecordingNotifier()
    scheduler = Scheduler(settings, [p], store, notifier)

    result = scheduler.run_once()

    assert len(result) == 1
    assert result[0].price_per_person == Decimal("2230")  # >1500 (standard cap), <=2300 (watchlist)
    assert [n["kind"] for n in notifier.sent] == ["watchlist_new_offer"]


def test_targeted_offer_above_2300_is_rejected_by_the_watchlist_cap(
    store: Store, write_hotel_watchlist: WriteHotelWatchlist
) -> None:
    """The watchlist's own ceiling still applies to TUI's targeted fetch too --
    a higher cap than the standard search's 1500, but not unlimited (mirrors
    the equivalent ITAKA/Wakacje.pl coverage for this same rule)."""
    settings = _tui_settings(write_hotel_watchlist)
    raw = watchlisted_raw_offer(
        discountPerPersonPrice="2400",
        originalPerPersonPrice="2400",
        discountFullPrice="4800",
        originalFullPrice="4800",
    )
    transport = FakeTransport([robots_response()])
    capture = FakeCapture(json.dumps({"offers": [raw]}))
    capture_price = FakeCapture(charter_realtime_body(raw))
    p = _TuiWatchlistOnlyStub(
        settings.providers["tui"],
        settings.filters,
        transport,
        capture,
        capture_price,
        sleep=lambda _: None,
        wall_clock=lambda: NOW,
    )
    notifier = _RecordingNotifier()
    scheduler = Scheduler(settings, [p], store, notifier)

    result = scheduler.run_once()

    assert result == []
    assert notifier.sent == []


def test_targeted_fetch_failure_never_stops_the_standard_search(
    store: Store, write_hotel_watchlist: WriteHotelWatchlist
) -> None:
    settings = _tui_settings(write_hotel_watchlist)

    class _FailingWatchlistStub(_TuiWatchlistOnlyStub):
        def fetch_watchlist_offers(self, entries: object) -> list[Offer]:
            raise RuntimeError("synthetic targeted-fetch failure")

    transport = FakeTransport([])
    p = _FailingWatchlistStub(
        settings.providers["tui"],
        settings.filters,
        transport,
        FakeCapture(NOT_AVAILABLE_BODY),
        FakeCapture(NOT_AVAILABLE_BODY),
        sleep=lambda _: None,
        wall_clock=lambda: NOW,
    )
    notifier = _RecordingNotifier()
    scheduler = Scheduler(settings, [p], store, notifier)

    result = scheduler.run_once()  # must not raise

    assert result == []
    assert notifier.sent == []


def test_same_real_offer_from_standard_and_targeted_fetch_alerts_only_once(
    store: Store, write_hotel_watchlist: WriteHotelWatchlist
) -> None:
    """A watched hotel can plausibly be returned by both `fetch()`'s own
    standard listing (rejected there only by the *standard* 1500 cap, so it
    still reaches the opportunistic `filter_watchlist_batch` pass on that same
    batch) and, in the same cycle, by `fetch_watchlist_offers()`'s separate
    query -- if TUI's `offerCode` is indeed stable across the two independent
    queries (see `tui_data.py`'s module docstring: this has not been
    independently verified live, but is the only sane assumption absent
    evidence otherwise). `Scheduler.run_once`/`filter_watchlist_batch`
    (keyed on (provider, offer_id)) must collapse this into exactly one
    alert and one history entry, never two -- this pins that contract for
    TUI's now-doubled fetch surface specifically."""
    settings = _tui_settings(write_hotel_watchlist)
    shared_offer_id = "SHARED0001"

    class _DoubleFetchStub(TuiProvider):
        """Standard `fetch()` returns the exact same real-world variant
        (same offer_id) that the real, capture-backed `fetch_watchlist_offers()`
        below also returns -- simulating TUI's own backend assigning a stable
        `offerCode` to the same trip regardless of which query found it."""

        def fetch(self) -> list[Offer]:
            return [
                Offer(
                    provider="tui",
                    offer_id=shared_offer_id,
                    hotel_name="Test Watchlist Hotel",
                    country=None,
                    departure_airport="LCJ",
                    departure_date=date(2026, 10, 1),
                    return_date=date(2026, 10, 9),
                    number_of_days=8,
                    number_of_people=2,
                    price_per_person=Decimal("2230"),
                    total_price=Decimal("2230") * 2,
                    currency="PLN",
                    board_type="all_inclusive",
                    url="https://www.tui.pl/wypoczynek/egipt/test-watchlist-hotel/x",
                    price_is_complete=True,
                )
            ]

    raw = watchlisted_raw_offer(offerCode=shared_offer_id, hotelCode="WATCH000")
    transport = FakeTransport([robots_response()])
    capture = FakeCapture(json.dumps({"offers": [raw]}))
    capture_price = FakeCapture(charter_realtime_body(raw))
    p = _DoubleFetchStub(
        settings.providers["tui"],
        settings.filters,
        transport,
        capture,
        capture_price,
        sleep=lambda _: None,
        wall_clock=lambda: NOW,
    )
    notifier = _RecordingNotifier()
    scheduler = Scheduler(settings, [p], store, notifier)

    result = scheduler.run_once()

    assert len(result) == 1
    assert [n["kind"] for n in notifier.sent] == ["watchlist_new_offer"]
    assert len(store.price_history("tui", shared_offer_id)) == 1


def test_alert_stays_single_even_if_the_two_fetches_disagree_on_offer_id(
    store: Store, write_hotel_watchlist: WriteHotelWatchlist
) -> None:
    """Worst case for `tui_data.py`'s own documented uncertainty ("[offerCode's]
    stability across repricing or repeated scans has not been independently
    verified"): the standard and targeted fetch describe the exact same
    real-world trip (identical hotel/dates/airport/nights/board/currency) but
    disagree on `offer_id`. `filter_watchlist_batch`'s (provider, offer_id)
    dedup can no longer help here -- both variants reach `finalize()` as
    distinct offers and each gets its own `price_history` row (a known,
    accepted limitation: history is keyed per offer_id, not per real-world
    trip). The alert itself must still not double up, because `Store.
    _enqueue_alert`'s `alert_state` is keyed by `duplicate_key` (the
    descriptive-fields group), independent of `offer_id`."""
    settings = _tui_settings(write_hotel_watchlist)

    class _DivergingOfferIdStub(TuiProvider):
        def fetch(self) -> list[Offer]:
            return [
                Offer(
                    provider="tui",
                    offer_id="STANDARD0001",
                    hotel_name="Test Watchlist Hotel",
                    country="TR",
                    destination="Turcja",
                    departure_airport="KTW",
                    departure_date=date(2026, 12, 4),
                    return_date=date(2026, 12, 10),
                    number_of_days=6,
                    number_of_people=2,
                    price_per_person=Decimal("2230"),
                    total_price=Decimal("2230") * 2,
                    currency="PLN",
                    board_type="FB",
                    url="https://www.tui.pl/wypoczynek/turcja/test-watchlist-hotel/x",
                    price_is_complete=True,
                )
            ]

    raw = watchlisted_raw_offer(offerCode="TARGETED0001", hotelCode="TARGETED")
    transport = FakeTransport([robots_response()])
    capture = FakeCapture(json.dumps({"offers": [raw]}))
    capture_price = FakeCapture(charter_realtime_body(raw))
    p = _DivergingOfferIdStub(
        settings.providers["tui"],
        settings.filters,
        transport,
        capture,
        capture_price,
        sleep=lambda _: None,
        wall_clock=lambda: NOW,
    )
    notifier = _RecordingNotifier()
    scheduler = Scheduler(settings, [p], store, notifier)

    result = scheduler.run_once()

    # Final ranked/deduplicated result also collapses to one (by duplicate_key).
    assert len(result) == 1
    # Exactly one alert, despite the two distinct offer_ids.
    assert [n["kind"] for n in notifier.sent] == ["watchlist_new_offer"]
    # Documented limitation: two separate history rows exist (one per
    # offer_id) even though only one alert fired -- this pins that current
    # behavior rather than silently assuming full history is merged.
    assert len(store.price_history("tui", "STANDARD0001")) == 1
    assert len(store.price_history("tui", "TARGETED0001")) == 1


def _enable_only_tui(settings: Settings) -> Settings:
    return replace(settings, providers={"tui": {**settings.providers["tui"], "enabled": True}})
