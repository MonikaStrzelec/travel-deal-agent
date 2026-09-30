"""Wakacje.pl's targeted, per-hotel watchlist fetch: entirely separate from
`fetch()`'s standard combined search query.

Injected HTTP only: these tests never contact wakacje.pl or use Playwright.
"""

import json
from dataclasses import replace
from typing import cast
from urllib.error import URLError

import pytest

from conftest import WriteHotelWatchlist
from travel_deal_agent.config import Settings, load_settings
from travel_deal_agent.config_types import HotelWatchlistEntry, ProviderConfig
from travel_deal_agent.models import Offer
from travel_deal_agent.notifications import Notifier
from travel_deal_agent.providers.http import Response
from travel_deal_agent.providers.wakacje import BASE, LISTING_PATH, WakacjeProvider
from travel_deal_agent.providers.wakacje_data import parse_listing
from travel_deal_agent.scheduler import Scheduler
from travel_deal_agent.storage import Notification, Store

CONFIG: ProviderConfig = {"enabled": True, "interval_seconds": 3600}

NEVERLAND_URL = (
    "https://www.wakacje.pl/wczasy/pickalbatros-jungle-aqua-park-resort-neverland-h17802/"
)

ALLOW_ROBOTS = "User-agent: *\nDisallow: /ajax/\nDisallow: /oferty/*?*\nDisallow: /*,*/"


def watchlist_entry(**overrides: object) -> HotelWatchlistEntry:
    base: dict[str, object] = {
        "name": "Pickalbatros Jungle Aqua Park Resort Neverland",
        "aliases": [],
        "country": "EG",
        "people": 2,
        "max_price_per_person": "2300",
        "min_nights": 7,
        "airports": ["LCJ", "WAW", "WMI", "KTW", "WRO"],
        "provider_listings": {"wakacje.pl": NEVERLAND_URL},
    }
    base.update(overrides)
    return cast(HotelWatchlistEntry, base)


def geo(name: str, slug: str) -> dict[str, object]:
    return {"name": name, "slug": slug, "urlName": slug, "id": 1}


def raw_offer(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": 17802001,
        "name": "Pickalbatros Jungle Aqua Park Resort Neverland",
        "urlName": "pickalbatros-jungle-aqua-park-resort-neverland",
        "place": {
            "country": geo("Egipt", "egipt"),
            "region": geo("Hurghada", "hurghada"),
            "city": geo("Hurghada", "hurghada"),
        },
        "category": 5,
        # Total for 2 adults (this URL never includes `za-osobe` -- see
        # `_watchlist_listing_path`/`price_view="total"`), i.e. 2000 PLN/person.
        "price": 4000,
        "originalCurrency": "PLN",
        "departureDate": "2026-11-10",
        "returnDate": "2026-11-18",
        "duration": 8,
        "departurePlace": "Łódź",
        "departurePlaceCode": "LCJ",
        "departurePlaces": ["Łódź"],
        "service": 1,
        "serviceDesc": "All Inclusive",
        "ratingValue": 9,
        "ratingReservationCount": 500,
    }
    base.update(overrides)
    return base


def listing_html(offers: list[dict[str, object]]) -> str:
    data = {
        "props": {
            "dehydratedState": {
                "queries": [
                    {
                        "queryKey": ["listingOffers", "x"],
                        "state": {"data": {"offers": {"data": offers, "count": len(offers)}}},
                    }
                ]
            }
        }
    }
    text = json.dumps(data, ensure_ascii=False)
    return f'<script id="__NEXT_DATA__" type="application/json">{text}</script>'


class FakeTransport:
    def __init__(self, responses: list[Response | Exception]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, timeout: float) -> Response:
        assert timeout > 0
        self.urls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def response(text: str, status: int = 200) -> Response:
    return Response(status, text, {})


def neverland_page_response() -> Response:
    return response(listing_html([raw_offer()]))


# --- Basic mechanics: one targeted request per hotel, cheapest-first sort ---


def test_fetch_watchlist_offers_is_a_separate_request_per_hotel(settings: Settings) -> None:
    transport = FakeTransport([response(ALLOW_ROBOTS), neverland_page_response()])
    provider = WakacjeProvider(CONFIG, settings.filters, transport, sleep=lambda _: None)

    offers = provider.fetch_watchlist_offers([watchlist_entry()])

    assert len(offers) == 1
    assert offers[0].hotel_name == "Pickalbatros Jungle Aqua Park Resort Neverland"


def test_targeted_fetch_reads_price_as_the_party_total_not_per_person(
    settings: Settings,
) -> None:
    # Live-confirmed 2026-09-28: this URL never includes `za-osobe`, so the
    # site's own confirmed default price view applies -- `price` is the total
    # for 2 adults, not per-person (see `price_view="total"` in wakacje.py).
    transport = FakeTransport([response(ALLOW_ROBOTS), neverland_page_response()])
    provider = WakacjeProvider(CONFIG, settings.filters, transport, sleep=lambda _: None)

    offers = provider.fetch_watchlist_offers([watchlist_entry()])

    assert offers[0].total_price == 4000
    assert offers[0].price_per_person == 2000


def test_non_flight_record_in_the_targeted_listing_is_skipped_not_fatal(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    # Live-confirmed 2026-09-28: the `?tanio` response for this exact hotel
    # included one real record with no departure airport at all (a non-flight
    # product) alongside nine valid ones -- the existing per-record skip in
    # parse_listing() already handles this; it must not fail the whole fetch.
    no_flight_record = raw_offer(
        id=1202913, departurePlace="", departurePlaceCode="", departurePlaces=[]
    )
    valid_record = raw_offer(id=937964)
    transport = FakeTransport(
        [response(ALLOW_ROBOTS), response(listing_html([no_flight_record, valid_record]))]
    )
    provider = WakacjeProvider(CONFIG, settings.filters, transport, sleep=lambda _: None)

    offers = provider.fetch_watchlist_offers([watchlist_entry()])

    assert len(offers) == 1
    assert offers[0].offer_id != ""
    assert offers[0].provider == "wakacje.pl"


def test_targeted_fetch_uses_the_confirmed_cheapest_first_sort_flag(settings: Settings) -> None:
    transport = FakeTransport([response(ALLOW_ROBOTS), neverland_page_response()])
    provider = WakacjeProvider(CONFIG, settings.filters, transport, sleep=lambda _: None)

    provider.fetch_watchlist_offers([watchlist_entry()])

    assert transport.urls == [
        BASE + "/robots.txt",
        BASE + "/wczasy/pickalbatros-jungle-aqua-park-resort-neverland-h17802/?tanio",
    ]


def test_configured_query_string_is_dropped_and_replaced(settings: Settings) -> None:
    # Arrange: the confirmed manual URL carried a UI-navigation tracking
    # parameter (?src=fromSearch); it must never reach the actual request.
    transport = FakeTransport([response(ALLOW_ROBOTS), neverland_page_response()])
    provider = WakacjeProvider(CONFIG, settings.filters, transport, sleep=lambda _: None)
    entry = watchlist_entry(provider_listings={"wakacje.pl": NEVERLAND_URL + "?src=fromSearch"})

    provider.fetch_watchlist_offers([entry])

    assert (
        transport.urls[1]
        == BASE + "/wczasy/pickalbatros-jungle-aqua-park-resort-neverland-h17802/?tanio"
    )
    assert "src" not in transport.urls[1]


def test_one_request_per_hotel_for_multiple_watched_hotels(settings: Settings) -> None:
    second_url = "https://www.wakacje.pl/wczasy/second-demo-hotel-h999/"
    transport = FakeTransport(
        [response(ALLOW_ROBOTS), neverland_page_response(), neverland_page_response()]
    )
    provider = WakacjeProvider(CONFIG, settings.filters, transport, sleep=lambda _: None)
    entries = [
        watchlist_entry(),
        watchlist_entry(
            name="Second Demo Hotel",
            provider_listings={"wakacje.pl": second_url},
        ),
    ]

    provider.fetch_watchlist_offers(entries)

    assert transport.urls == [
        BASE + "/robots.txt",
        BASE + "/wczasy/pickalbatros-jungle-aqua-park-resort-neverland-h17802/?tanio",
        BASE + "/wczasy/second-demo-hotel-h999/?tanio",
    ]


def test_no_entries_with_a_wakacje_listing_means_no_requests_at_all(settings: Settings) -> None:
    transport = FakeTransport([])
    provider = WakacjeProvider(CONFIG, settings.filters, transport, sleep=lambda _: None)

    offers = provider.fetch_watchlist_offers([watchlist_entry(provider_listings={})])

    assert offers == []
    assert transport.urls == []


def test_empty_watchlist_means_no_requests_at_all(settings: Settings) -> None:
    transport = FakeTransport([])
    provider = WakacjeProvider(CONFIG, settings.filters, transport, sleep=lambda _: None)

    offers = provider.fetch_watchlist_offers([])

    assert offers == []
    assert transport.urls == []


# --- Isolation: never touches the standard fetch()'s own budget/state -------


def test_targeted_fetch_never_touches_the_standard_search_query(settings: Settings) -> None:
    transport = FakeTransport([response(ALLOW_ROBOTS), neverland_page_response()])
    provider = WakacjeProvider(
        settings.providers["wakacje.pl"], settings.filters, transport, sleep=lambda _: None
    )

    provider.fetch_watchlist_offers([watchlist_entry()])

    assert all(LISTING_PATH + "?" not in url or "tanio" in url for url in transport.urls)
    assert all("all-inclusive" not in url for url in transport.urls)


# --- Failure isolation: one hotel's failure never breaks the others ---------


def test_robots_blocked_hotel_is_skipped_but_others_still_fetched(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    blocking_robots = (
        "User-agent: *\nDisallow: /wczasy/pickalbatros-jungle-aqua-park-resort-neverland-h17802/"
    )
    second_url = "https://www.wakacje.pl/wczasy/second-demo-hotel-h999/"
    transport = FakeTransport([response(blocking_robots), neverland_page_response()])
    provider = WakacjeProvider(CONFIG, settings.filters, transport, sleep=lambda _: None)
    entries = [
        watchlist_entry(),
        watchlist_entry(name="Second Demo Hotel", provider_listings={"wakacje.pl": second_url}),
    ]

    offers = provider.fetch_watchlist_offers(entries)

    # Only the second hotel's listing was actually requested and parsed.
    assert transport.urls == [
        BASE + "/robots.txt",
        BASE + "/wczasy/second-demo-hotel-h999/?tanio",
    ]
    assert len(offers) == 1
    assert "Neverland" not in caplog.text or "skipped" in caplog.text


def test_transient_network_error_for_one_hotel_does_not_stop_the_others(
    settings: Settings,
) -> None:
    second_url = "https://www.wakacje.pl/wczasy/second-demo-hotel-h999/"
    transport = FakeTransport(
        [response(ALLOW_ROBOTS), URLError("synthetic failure"), neverland_page_response()]
    )
    provider = WakacjeProvider(CONFIG, settings.filters, transport, sleep=lambda _: None)
    entries = [
        watchlist_entry(),
        watchlist_entry(name="Second Demo Hotel", provider_listings={"wakacje.pl": second_url}),
    ]

    offers = provider.fetch_watchlist_offers(entries)

    assert len(offers) == 1
    assert len(transport.urls) == 3


def test_wrong_host_url_is_rejected_defensively(settings: Settings) -> None:
    transport = FakeTransport([response(ALLOW_ROBOTS)])
    provider = WakacjeProvider(CONFIG, settings.filters, transport, sleep=lambda _: None)
    entry = watchlist_entry(
        provider_listings={"wakacje.pl": "https://evil.example/wczasy/hotel-h1/"}
    )

    offers = provider.fetch_watchlist_offers([entry])

    assert offers == []
    # Only robots.txt was fetched; the bad-host URL was never requested.
    assert transport.urls == [BASE + "/robots.txt"]


def test_robots_txt_failure_itself_still_raises(settings: Settings) -> None:
    # Unlike a single hotel's own listing, a failure reading the shared
    # robots.txt fails closed, same as fetch() -- the caller (Scheduler)
    # isolates this, not the method itself.
    transport = FakeTransport([response("", status=503)])
    provider = WakacjeProvider(CONFIG, settings.filters, transport, sleep=lambda _: None)

    with pytest.raises(ValueError):
        provider.fetch_watchlist_offers([watchlist_entry()])


def test_module_still_never_imports_playwright() -> None:
    from pathlib import Path

    source = Path(
        Path(__file__).parent.parent / "travel_deal_agent" / "providers" / "wakacje.py"
    ).read_text(encoding="utf-8")
    assert "import playwright" not in source.lower()
    assert "from playwright" not in source.lower()


# --- End-to-end: Scheduler wiring, price band, dedup, failure isolation -----


class _RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> None:
        self.sent.append(notification)


class _WakacjeStub(WakacjeProvider):
    """A WakacjeProvider whose standard fetch() never touches the network --
    only `fetch_watchlist_offers()` (still transport-backed) is under test."""

    def fetch(self) -> list[Offer]:
        return []


class _WakacjeOverlapStub(WakacjeProvider):
    """Simulates the same Neverland offer appearing in *both* the standard
    listing and the targeted per-hotel listing on the same cycle."""

    def fetch(self) -> list[Offer]:
        return parse_listing(listing_html([raw_offer()]), self.wall_clock())


class _WakacjeFailingWatchlistStub(_WakacjeStub):
    """Its standard fetch() needs no network (see _WakacjeStub); its targeted
    watchlist fetch always raises, to prove that failure never reaches the
    standard search."""

    def fetch_watchlist_offers(self, entries: object) -> list[Offer]:
        raise RuntimeError("synthetic targeted-fetch failure")


def _wakacje_settings(write_hotel_watchlist: WriteHotelWatchlist) -> Settings:
    write_hotel_watchlist([dict(watchlist_entry())])
    settings = load_settings()
    return replace(settings, providers={"wakacje.pl": {"enabled": True, "interval_seconds": 3600}})


def test_targeted_offer_priced_above_1500_and_at_most_2300_is_accepted(
    store: Store, write_hotel_watchlist: WriteHotelWatchlist
) -> None:
    settings = _wakacje_settings(write_hotel_watchlist)
    transport = FakeTransport([response(ALLOW_ROBOTS), neverland_page_response()])
    provider = _WakacjeStub(
        settings.providers["wakacje.pl"], settings.filters, transport, sleep=lambda _: None
    )
    notifier = _RecordingNotifier()
    scheduler = Scheduler(settings, [provider], store, notifier)

    result = scheduler.run_once()

    assert len(result) == 1
    assert result[0].price_per_person == 2000  # >1500 (standard cap), <=2300 (watchlist cap)
    assert [n["kind"] for n in notifier.sent] == ["watchlist_new_offer"]


def test_targeted_offer_above_2300_is_rejected_by_the_watchlist_cap(
    store: Store, write_hotel_watchlist: WriteHotelWatchlist
) -> None:
    """The watchlist's own ceiling still applies -- a higher cap than the
    standard search's 1500, but not unlimited."""
    settings = _wakacje_settings(write_hotel_watchlist)
    # 4800 PLN total / 2 people = 2400 PLN/person, above the watchlist's 2300 cap.
    transport = FakeTransport(
        [response(ALLOW_ROBOTS), response(listing_html([raw_offer(price=4800)]))]
    )
    provider = _WakacjeStub(
        settings.providers["wakacje.pl"], settings.filters, transport, sleep=lambda _: None
    )
    notifier = _RecordingNotifier()
    scheduler = Scheduler(settings, [provider], store, notifier)

    result = scheduler.run_once()

    assert result == []
    assert notifier.sent == []


def test_offer_in_both_standard_and_targeted_fetch_alerts_only_once(
    store: Store, write_hotel_watchlist: WriteHotelWatchlist
) -> None:
    settings = _wakacje_settings(write_hotel_watchlist)
    # Only robots.txt + the targeted listing are fetched over the network --
    # _WakacjeOverlapStub.fetch() (the "standard" search) builds its result
    # from the very same fixture HTML directly, no transport call needed.
    transport = FakeTransport([response(ALLOW_ROBOTS), neverland_page_response()])
    provider = _WakacjeOverlapStub(
        settings.providers["wakacje.pl"], settings.filters, transport, sleep=lambda _: None
    )
    notifier = _RecordingNotifier()
    scheduler = Scheduler(settings, [provider], store, notifier)

    result = scheduler.run_once()

    # The identical offer (same raw record -> same variant_identity) was
    # produced by both the "standard" fetch() and the targeted fetch --
    # exactly one result, exactly one notification.
    assert len(result) == 1
    assert len(notifier.sent) == 1


def test_targeted_fetch_failure_never_stops_the_standard_search(
    store: Store, write_hotel_watchlist: WriteHotelWatchlist, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _wakacje_settings(write_hotel_watchlist)
    # fetch() needs no network here (see _WakacjeStub); this isolates the
    # assertion to exactly what's under test -- the targeted fetch's failure.
    transport = FakeTransport([])
    provider = _WakacjeFailingWatchlistStub(
        settings.providers["wakacje.pl"], settings.filters, transport
    )
    notifier = _RecordingNotifier()
    scheduler = Scheduler(settings, [provider], store, notifier)

    result = scheduler.run_once()  # must not raise

    assert result == []
    assert notifier.sent == []
