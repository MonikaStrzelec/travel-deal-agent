"""Cross-cutting watchlist guarantees found by the pre-deployment audit.

Boundary prices, airport identity, the full alert sequence, the same hotel at
several providers, per-provider failure isolation, config edge cases and the
diagnostic logs. Everything here is offline: injected fakes, temporary
databases, no network.
"""

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import TypeAdapter
from test_google_places import FakeTransport as GoogleTransport
from test_google_places import _place
from test_itaka_watchlist import ROBOTS, entry, ok, page, rate
from test_itaka_watchlist import FakeTransport as ItakaTransport
from test_itaka_watchlist import provider as itaka_provider
from test_tui_watchlist_destination import scoped
from test_tui_watchlist_pagination import PagedCapture, make_provider, page_body

from conftest import WriteHotelWatchlist
from travel_deal_agent.config import Settings, load_hotel_watchlist, load_settings
from travel_deal_agent.config_types import HotelWatchlistEntry
from travel_deal_agent.models import Offer
from travel_deal_agent.notification_content import NotificationMessage
from travel_deal_agent.notifications import Notifier
from travel_deal_agent.providers.external_rating import GoogleRatingProvider
from travel_deal_agent.providers.itaka_query import build_watchlist_path
from travel_deal_agent.providers.mock import MockProvider
from travel_deal_agent.providers.tui_query import build_watchlist_search_path
from travel_deal_agent.scheduler import Scheduler
from travel_deal_agent.storage import Notification, Store
from travel_deal_agent.watchlist import matches

HOTEL = "Pickalbatros Jungle Aqua Park Resort Neverland"


def _entry(**overrides: object) -> HotelWatchlistEntry:
    base: dict[str, object] = {
        "name": HOTEL,
        "aliases": [],
        "country": "EG",
        "people": 2,
        "max_price_per_person": "2300",
        "min_nights": 7,
        "airports": ["LCJ", "WAW", "WMI", "KTW", "WRO"],
    }
    base.update(overrides)
    return cast(HotelWatchlistEntry, base)


def _neverland(offer: Offer, **overrides: object) -> Offer:
    assert offer.departure_date is not None
    base = replace(
        offer,
        provider="wakacje.pl",
        offer_id="neverland-1",
        hotel_name=HOTEL,
        country="EG",
        destination="Hurghada",
        departure_airport="LCJ",
        number_of_people=2,
        price_per_person=Decimal("2300"),
        total_price=Decimal("4600"),
        return_date=offer.departure_date + timedelta(days=7),
        price_is_complete=True,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


class _Recorder(Notifier):
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> None:
        self.sent.append(notification)


# --- price ceiling: Decimal, per person ---------------------------------------


@pytest.mark.parametrize(
    "price,expected",
    [
        ("2299", True),
        ("2300", True),
        ("2300.00", True),
        ("2300.01", False),
        ("2301", False),
        ("0", False),
    ],
)
def test_price_ceiling_is_an_exact_decimal_comparison(
    offer: Offer, price: str, expected: bool
) -> None:
    candidate = _neverland(offer, price_per_person=Decimal(price))

    assert matches(candidate, _entry(), []) is expected


def test_party_size_other_than_the_watched_one_is_rejected(offer: Offer) -> None:
    assert matches(_neverland(offer, number_of_people=3), _entry(), []) is False
    assert matches(_neverland(offer, number_of_people=1), _entry(), []) is False


@pytest.mark.parametrize("nights,expected", [(6, False), (7, True), (9, True), (10, False)])
def test_stay_length_bounds_are_inclusive(offer: Offer, nights: int, expected: bool) -> None:
    assert offer.departure_date is not None
    candidate = _neverland(offer, return_date=offer.departure_date + timedelta(days=nights))

    assert matches(candidate, _entry(min_nights=7, max_nights=9), []) is expected


def test_no_minimum_nights_accepts_a_short_stay(offer: Offer) -> None:
    assert offer.departure_date is not None
    candidate = _neverland(offer, return_date=offer.departure_date + timedelta(days=2))

    assert matches(candidate, _entry(min_nights=None), []) is True


# --- airports: IATA identity ---------------------------------------------------


@pytest.mark.parametrize(
    "watched,offered,expected",
    [
        (["WAW"], "WMI", False),
        (["WMI"], "WAW", False),
        (["WAW"], "WAW", True),
        (["LCJ", "WAW", "WMI", "KTW", "WRO"], "KRK", False),
        (["LCJ", "WAW", "WMI", "KTW", "WRO"], "WMI", True),
    ],
)
def test_warsaw_airports_are_never_confused(
    offer: Offer, watched: list[str], offered: str, expected: bool
) -> None:
    candidate = _neverland(offer, departure_airport=offered)

    assert matches(candidate, _entry(airports=watched), []) is expected


def test_watchlist_airport_without_offers_is_not_an_error(offer: Offer) -> None:
    # LCJ is watched but the provider only lists KTW: nothing errors, KTW matches.
    assert matches(_neverland(offer, departure_airport="KTW"), _entry(), []) is True


def test_tui_never_queries_the_disabled_modlin_airport() -> None:
    path = build_watchlist_search_path([scoped("A", "HRG")])

    airports = parse_qs(urlsplit(path).query)["q"][0].split(":")
    selected = [airports[i + 1] for i, token in enumerate(airports) if token == "a"]
    assert selected == ["KTW", "LCJ", "WAW", "WRO"]  # WMI is excluded, not confused with WAW


def test_tui_entry_with_only_a_disabled_airport_fails_loudly() -> None:
    with pytest.raises(ValueError, match="airport"):
        build_watchlist_search_path([scoped("A", "HRG", airports=["WMI"])])


def test_itaka_query_keeps_every_watched_airport_distinct() -> None:
    url = build_watchlist_path("egipt", [entry("A", "egipt")])

    query = parse_qs(urlsplit(url).query)
    assert query["departuresByPlane"] == ["KTW,LCJ,WAW,WMI,WRO"]
    assert query["adults[0]"] == ["2"]
    assert query["priceTo"] == ["2300"]  # per person, exactly the watchlist ceiling


# --- the complete alert sequence, with the watchlist prefix --------------------


def test_watchlist_alert_sequence_matches_the_standard_policy(
    offer: Offer, settings: Settings
) -> None:
    now = [datetime(2026, 9, 24, 7, tzinfo=timezone.utc)]
    with Store(
        settings.database,
        alert_rearm_after=timedelta(hours=24),
        price_drop_threshold=settings.price_drop_threshold,
        clock=lambda: now[0],
    ) as store:
        watched = _neverland(offer, price_per_person=Decimal("2300"))

        def observe(price: str) -> list[str]:
            now[0] += timedelta(hours=1)
            return store.observe(
                replace(watched, price_per_person=Decimal(price)), True, kind_prefix="watchlist_"
            )

        assert observe("2300") == ["watchlist_new_offer"]
        assert observe("2300") == []  # unchanged repeat
        assert observe("2500") == ["price_changed"]  # increase: recorded, no alert
        assert observe("2350") == ["price_changed", "watchlist_price_drop"]  # not below 2300
        assert observe("2000") == ["price_changed", "watchlist_new_low"]
        assert observe("2100") == ["price_changed"]
        now[0] += timedelta(hours=30)  # gone past the re-arm window, then back
        assert observe("2100") == ["watchlist_returned"]

        assert [n["kind"] for n in store.pending()] == [
            "watchlist_new_offer",
            "watchlist_price_drop",
            "watchlist_new_low",
            "watchlist_returned",
        ]
        for notification in store.pending():
            text = NotificationMessage.from_notification(notification).render()
            assert "Obserwowany hotel" in text


# --- the same hotel at three providers -----------------------------------------


def test_same_hotel_at_three_providers_keeps_separate_identity_and_history(
    offer: Offer, settings: Settings
) -> None:
    offers = [
        _neverland(
            offer,
            provider=name,
            offer_id=f"{name}-1",
            destination=f"{name} resort",
            price_per_person=Decimal(price),
        )
        for name, price in (("wakacje.pl", "2100"), ("tui", "2200"), ("itaka", "2250"))
    ]
    with Store(settings.database) as store:
        events = [store.observe(o, True, kind_prefix="watchlist_") for o in offers]
        # Every provider has its own snapshot and its own price series...
        stored = [store.get_offer(o.provider, o.offer_id) for o in offers]
        assert [(s.provider, s.offer_id) for s in stored if s] == [
            (o.provider, o.offer_id) for o in offers
        ]
        assert [store.price_history(o.provider, o.offer_id) for o in offers] == [
            [Decimal("2100")],
            [Decimal("2200")],
            [Decimal("2250")],
        ]
        # ...and (differing in destination/price) each raises its own alert.
        assert events == [["watchlist_new_offer"]] * 3
        payloads = [TypeAdapter(Offer).validate_json(n["payload"]) for n in store.pending()]
        assert [p.provider for p in payloads] == ["wakacje.pl", "tui", "itaka"]


def test_identical_trip_at_two_providers_keeps_history_apart_but_alerts_once(
    offer: Offer, settings: Settings
) -> None:
    """Documents the existing conservative policy: history is per provider, only
    the alert is grouped by the trip's `duplicate_key` (see storage._enqueue_alert)."""
    first = _neverland(offer, provider="wakacje.pl", offer_id="w-1")
    second = _neverland(offer, provider="tui", offer_id="t-1")
    with Store(settings.database) as store:
        assert store.observe(first, True, kind_prefix="watchlist_") == ["watchlist_new_offer"]
        assert store.observe(second, True, kind_prefix="watchlist_") == []

        assert store.get_offer("wakacje.pl", "w-1") is not None
        assert store.get_offer("tui", "t-1") is not None
        assert len(store.pending()) == 1


def test_google_rating_is_shared_across_providers_for_the_same_hotel(
    offer: Offer, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    hotel = _neverland(offer, provider="wakacje.pl", destination="Hurghada")
    with Store(settings.database) as store:
        transport = GoogleTransport([_place(name=HOTEL, place_id="p-1", address="Hurghada, Egypt")])
        google = GoogleRatingProvider(store=store, transport=transport)

        ratings = [
            google.verify(replace(hotel, provider=name, offer_id=f"{name}-1"))
            for name in ("wakacje.pl", "tui", "itaka")
        ]

        assert len(transport.queries) == 1
        assert all(r is not None and r.external_id == "p-1" for r in ratings)


# --- per-provider failure isolation --------------------------------------------


class _Stub(MockProvider):
    """A provider with a scripted standard fetch and targeted fetch."""

    def __init__(
        self,
        name: str,
        standard: list[Offer] | None = None,
        targeted: list[Offer] | None = None,
        error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.name = name
        self._standard = standard or []
        self._targeted = targeted or []
        self._error = error

    def fetch(self) -> list[Offer]:
        return self._standard

    def fetch_watchlist_offers(self, entries: object) -> list[Offer]:
        if self._error is not None:
            raise self._error
        return self._targeted


def _scheduler(
    settings: Settings, store: Store, providers: list[_Stub], notifier: Notifier
) -> Scheduler:
    settings = replace(
        settings,
        providers={p.name: {"enabled": True, "interval_seconds": 3600} for p in providers},
    )
    return Scheduler(settings, providers, store, notifier, external_providers=[])


@pytest.mark.parametrize("failing", ["tui", "itaka"])
@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("synthetic timeout"),
        ValueError("synthetic HTTP 500 / parser error"),
        RuntimeError("synthetic bug"),
    ],
)
def test_a_failing_targeted_fetch_never_stops_other_providers(
    offer: Offer,
    store: Store,
    write_hotel_watchlist: WriteHotelWatchlist,
    caplog: pytest.LogCaptureFixture,
    failing: str,
    error: Exception,
) -> None:
    write_hotel_watchlist([dict(_entry())])
    settings = load_settings()
    healthy = "wakacje.pl"
    stubs = [
        _Stub(failing, error=error),
        _Stub(healthy, targeted=[_neverland(offer, provider=healthy)]),
    ]
    notifier = _Recorder()

    with caplog.at_level(logging.ERROR):
        result = _scheduler(settings, store, stubs, notifier).run_once()

    assert [(o.provider, o.offer_id) for o in result] == [(healthy, "neverland-1")]
    assert [n["kind"] for n in notifier.sent] == ["watchlist_new_offer"]
    assert f"Targeted watchlist fetch failed for provider {failing}" in caplog.text


def test_offer_found_by_both_the_opportunistic_and_targeted_pass_alerts_once(
    offer: Offer, store: Store, write_hotel_watchlist: WriteHotelWatchlist
) -> None:
    write_hotel_watchlist([dict(_entry())])
    settings = load_settings()
    watched = _neverland(offer, provider="tui")
    stub = _Stub("tui", standard=[watched], targeted=[watched, watched])
    notifier = _Recorder()

    result = _scheduler(settings, store, [stub], notifier).run_once()

    assert len(result) == 1
    assert [n["kind"] for n in notifier.sent] == ["watchlist_new_offer"]
    assert store.price_history("tui", "neverland-1") == [Decimal("2300")]


# --- config edge cases ---------------------------------------------------------


def _load(
    write: WriteHotelWatchlist, entries: list[dict[str, object]]
) -> list[HotelWatchlistEntry]:
    return load_hotel_watchlist(write(entries))


def test_empty_and_unknown_provider_destinations_are_legal(
    write_hotel_watchlist: WriteHotelWatchlist,
) -> None:
    loaded = _load(
        write_hotel_watchlist,
        [
            dict(_entry(name="A", provider_destinations={})),
            dict(_entry(name="B", provider_destinations={"future-provider": "SOME-CODE"})),
        ],
    )

    assert [e["name"] for e in loaded] == ["A", "B"]


@pytest.mark.parametrize(
    "destinations",
    [{"itaka": "Egipt"}, {"itaka": "egipt_x"}, {"itaka": "e"}, {"tui": "hrg"}, {"tui": "H"}],
)
def test_malformed_destination_disables_the_watchlist_but_not_the_application(
    write_hotel_watchlist: WriteHotelWatchlist,
    caplog: pytest.LogCaptureFixture,
    destinations: dict[str, str],
) -> None:
    write_hotel_watchlist([dict(_entry(provider_destinations=destinations))])

    with caplog.at_level(logging.ERROR):
        settings = load_settings()

    assert settings.hotel_watchlist == []
    assert settings.filters["max_price"] == "1500"  # the standard search is untouched
    assert "Invalid hotel watchlist" in caplog.text


@pytest.mark.parametrize(
    "listing",
    ["http://www.wakacje.pl/x-h1/", "https://evil.example/x-h1/", "www.wakacje.pl/x-h1/"],
)
def test_wakacje_listing_must_be_an_https_wakacje_url(
    write_hotel_watchlist: WriteHotelWatchlist, listing: str
) -> None:
    path = write_hotel_watchlist([dict(_entry(provider_listings={"wakacje.pl": listing}))])

    assert load_hotel_watchlist(path) == []


def test_hotels_with_different_destinations_and_limits_load_together(
    write_hotel_watchlist: WriteHotelWatchlist,
) -> None:
    loaded = _load(
        write_hotel_watchlist,
        [
            dict(
                _entry(
                    name="A",
                    max_price_per_person="2000",
                    provider_destinations={"tui": "HRG", "itaka": "egipt"},
                )
            ),
            dict(
                _entry(
                    name="B",
                    max_price_per_person="3100",
                    provider_destinations={"tui": "RMF", "itaka": "egipt"},
                )
            ),
            dict(
                _entry(
                    name="C",
                    max_price_per_person="1800",
                    provider_destinations={"tui": "AYT", "itaka": "turcja"},
                )
            ),
        ],
    )

    assert [e["max_price_per_person"] for e in loaded] == ["2000", "3100", "1800"]
    path = build_watchlist_search_path(loaded)
    assert "#6200" in parse_qs(urlsplit(path).query)["q"][0].split(":")  # widest, x2 people
    egypt = build_watchlist_path("egipt", loaded[:2])
    assert parse_qs(urlsplit(egypt).query)["priceTo"] == ["3100"]


def test_the_shipped_example_is_a_valid_watchlist() -> None:
    example = Path(__file__).resolve().parent.parent / "hotel_watchlist.example.json"

    loaded = load_hotel_watchlist(example)

    assert len(loaded) == 1
    assert loaded[0]["provider_destinations"] == {"tui": "HRG", "itaka": "egipt"}


# --- diagnostic logs -----------------------------------------------------------


def test_rejected_watchlist_candidate_is_logged_with_its_reason(
    offer: Offer, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="travel_deal_agent.watchlist"):
        matches(_neverland(offer, price_per_person=Decimal("2301")), _entry(), [])
        matches(_neverland(offer, departure_airport="KRK"), _entry(), [])
        matches(replace(_neverland(offer), hotel_name="Some Other Hotel"), _entry(), [])

    assert "outside (0, 2300]" in caplog.text
    assert "airport KRK not watched" in caplog.text
    assert "Some Other Hotel" not in caplog.text  # other hotels are never logged


def test_tui_targeted_search_logs_its_destination(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    capture = PagedCapture([page_body(1, 1, [])])

    with caplog.at_level(logging.INFO, logger="travel_deal_agent.providers.tui"):
        make_provider(settings, capture).fetch_watchlist_offers([scoped("A", "HRG")])

    assert "TUI watchlist search: destination(s) ['HRG'] for 1 hotel(s)" in caplog.text
    assert "0 offer(s) matched by hotel identity" in caplog.text


def test_itaka_targeted_search_logs_its_destination(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    transport = ItakaTransport([ROBOTS, ok(page([rate("r1", "Other", 1900)], count=1))])
    p = itaka_provider(settings, transport)

    with caplog.at_level(logging.INFO, logger="travel_deal_agent.providers.itaka"):
        p.fetch_watchlist_offers([entry("Hotel A", "egipt")])

    assert "ITAKA watchlist search: destination 'egipt' for 1 hotel(s)" in caplog.text
    assert "0 matched by hotel identity" in caplog.text
