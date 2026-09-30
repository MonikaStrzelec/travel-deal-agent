"""Destination-scoped ITAKA watchlist fetch (`provider_destinations["itaka"]`).

Confirmed live 2026-09-30: `/wyniki-wyszukiwania/wakacje/<slug>/` with `priceTo` and
`order=priceAsc` returns the same `__NEXT_DATA__` payload `parse_page` reads.
Injected transport fakes; no network.
"""

import json
import logging
from copy import deepcopy
from datetime import date
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import WriteHotelWatchlist
from travel_deal_agent.config import Settings, load_hotel_watchlist
from travel_deal_agent.config_types import HotelWatchlistEntry, ProviderConfig
from travel_deal_agent.providers.http import Response
from travel_deal_agent.providers.itaka import BASE, ItakaProvider
from travel_deal_agent.providers.itaka_query import build_watchlist_path, destination_groups
from travel_deal_agent.watchlist import matches

TODAY = date(2026, 9, 30)


def entry(
    name: str, slug: str | None, cap: str = "2300", **overrides: object
) -> HotelWatchlistEntry:
    base: dict[str, object] = {
        "name": name,
        "aliases": [],
        "people": 2,
        "max_price_per_person": cap,
        "min_nights": 7,
        "airports": ["LCJ", "WAW", "WMI", "KTW", "WRO"],
    }
    if slug is not None:
        base["provider_destinations"] = {"itaka": slug}
    base.update(overrides)
    return cast(HotelWatchlistEntry, base)


def rate(rate_id: str, hotel: str, per_person: int) -> dict[str, object]:
    cents = per_person * 100
    result: dict[str, object] = {
        "supplier": "itaka",
        "supplierObjectId": "TEST",
        "rateType": "holidays",
        "currency": "PLN",
        "participantGroups": [
            {
                "price": cents * 2,
                "rateId": rate_id,
                "participants": [
                    {"price": cents, "type": "adult", "additionalPayments": []},
                    {"price": cents, "type": "adult", "additionalPayments": []},
                ],
            }
        ],
        "duration": {"days": 8},
        "segments": [
            {
                "type": "flight",
                "beginDate": "2030-10-01",
                "endDate": "2030-10-01",
                "departure": {"title": "Łódź"},
                "destination": {"title": "Burgas"},
            },
            {
                "type": "hotel",
                "beginDate": "2030-10-01",
                "endDate": "2030-10-08",
                "participantGroups": [
                    {
                        "meal": {"id": "A", "title": "All inclusive"},
                        "room": {"id": "DBL", "title": "Double"},
                        "baseRoomCode": "D2",
                    }
                ],
                "content": {
                    "title": hotel,
                    "hotelRating": 40,
                    "reviews": {"customersRating": 5.4, "reviewsNumber": 123},
                    "geographicalIdentifiers": [
                        {"id": "bulgaria", "title": "Bułgaria", "type": "country"}
                    ],
                },
            },
            {
                "type": "flight",
                "beginDate": "2030-10-08",
                "endDate": "2030-10-09",
                "departure": {"title": "Burgas"},
                "destination": {"title": "Łódź"},
            },
        ],
    }
    return deepcopy(result)


def page(rates: list[dict[str, object]], *, skip: int = 0, take: int = 15, count: int) -> str:
    data = {
        "props": {
            "pageProps": {
                "initialQueryState": {
                    "queries": [
                        {
                            "queryKey": ["rates", {"skip": skip, "take": take}],
                            "state": {
                                "data": {
                                    "main": {"multiRoomRates": {"list": rates, "ratesCount": count}}
                                }
                            },
                        }
                    ]
                }
            }
        }
    }
    rate_ids = [str(r["participantGroups"][0]["rateId"]) for r in rates]  # type: ignore[index]
    anchors = "".join(
        f'<a href="/wczasy/test,{rate_id}/?id%5B0%5D={rate_id}">d</a>' for rate_id in rate_ids
    )
    body = json.dumps(data)
    return f'<script id="__NEXT_DATA__" type="application/json">{body}</script>{anchors}'


def ok(text: str) -> Response:
    return Response(200, text, {})


ROBOTS = ok("User-agent: *\nDisallow: /api*")
NO_DETAIL = ok("<html>no detail data</html>")


class FakeTransport:
    def __init__(self, responses: list[Response | None]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, timeout: float) -> Response:
        self.urls.append(url)
        item = self.responses.pop(0)
        if item is None:
            raise TimeoutError("synthetic timeout")
        return item


def provider(settings: Settings, transport: FakeTransport, **cfg: object) -> ItakaProvider:
    configuration = cast(ProviderConfig, {**settings.providers["itaka"], **cfg})
    return ItakaProvider(
        configuration, settings.filters, transport, sleep=lambda _: None, today=lambda: TODAY
    )


def query_of(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


# --- URL builder ---------------------------------------------------------------


def test_path_uses_the_slug_the_widest_cap_and_the_union_of_airports() -> None:
    entries = [
        entry("A", "sluga", cap="2000", airports=["WAW", "KTW"]),
        entry("B", "sluga", cap="2300", airports=["KTW", "LCJ"]),
    ]

    url = build_watchlist_path("sluga", entries)

    assert urlsplit(url).path == "/wyniki-wyszukiwania/wakacje/sluga/"
    assert query_of(url) == {
        "departuresByPlane": ["KTW,LCJ,WAW"],
        "durationMin": ["7"],
        "adults[0]": ["2"],
        "order": ["priceAsc"],
        "priceTo": ["2300"],
    }


def test_duration_floor_is_the_loosest_and_absent_if_any_entry_has_none() -> None:
    both = [entry("A", "s1", min_nights=9), entry("B", "s1", min_nights=7)]
    one_open = [entry("A", "s1", min_nights=9), entry("B", "s1", min_nights=None)]

    assert query_of(build_watchlist_path("s1", both))["durationMin"] == ["7"]
    assert "durationMin" not in query_of(build_watchlist_path("s1", one_open))


def test_later_pages_add_the_page_parameter() -> None:
    assert query_of(build_watchlist_path("s1", [entry("A", "s1")], page=2))["page"] == ["2"]


@pytest.mark.parametrize("slug", ["", "Egipt", "a/b", "a b", "a?x=1"])
def test_invalid_slug_is_rejected(slug: str) -> None:
    with pytest.raises(ValueError, match="slug"):
        build_watchlist_path(slug, [entry("A", "s1")])


def test_groups_by_slug_and_skip_unscoped_or_other_party_size() -> None:
    a, b, c = entry("A", "s1"), entry("B", "s2"), entry("C", "s1")
    groups = destination_groups([a, b, c, entry("D", None), entry("E", "s3", people=3)])

    assert groups == {"s1": [a, c], "s2": [b]}


# --- provider ------------------------------------------------------------------


def test_no_destination_means_no_request(settings: Settings) -> None:
    transport = FakeTransport([])

    assert provider(settings, transport).fetch_watchlist_offers([entry("A", None)]) == []
    assert transport.urls == []


def test_one_hotel_costs_robots_one_page_and_one_detail_with_its_own_cap(
    settings: Settings,
) -> None:
    body = page([rate("r1", "Watched Hotel", 2200)], count=1)
    transport = FakeTransport([ROBOTS, ok(body), NO_DETAIL])

    offers = provider(settings, transport).fetch_watchlist_offers([entry("Watched Hotel", "sluga")])

    assert [o.hotel_name for o in offers] == ["Watched Hotel"]
    assert len(transport.urls) == 3
    assert transport.urls[0] == BASE + "/robots.txt"
    listing = urlsplit(transport.urls[1])
    assert listing.path == "/wyniki-wyszukiwania/wakacje/sluga/"
    assert query_of(transport.urls[1])["priceTo"] == ["2300"]  # not the global 1500
    assert "r1" in transport.urls[2]  # the detail request went to the watched offer


def test_detail_request_goes_to_an_identity_match_not_a_cheaper_stranger(
    settings: Settings,
) -> None:
    body = page([rate("r1", "Other Hotel", 1800), rate("r2", "Watched Hotel", 2200)], count=2)
    transport = FakeTransport([ROBOTS, ok(body), NO_DETAIL])

    provider(settings, transport).fetch_watchlist_offers([entry("Watched Hotel", "sluga")])

    assert "r2" in transport.urls[-1]


def test_hotels_sharing_a_slug_share_one_query(settings: Settings) -> None:
    body = page([rate("r1", "Hotel A", 1900), rate("r2", "Hotel B", 2250)], count=2)
    transport = FakeTransport([ROBOTS, ok(body), NO_DETAIL])
    entries = [entry("Hotel A", "sluga", cap="2000"), entry("Hotel B", "sluga", cap="2300")]

    offers = provider(settings, transport, max_detail_requests=1).fetch_watchlist_offers(entries)

    assert len(offers) == 2
    assert len(transport.urls) == 3  # robots + one shared page + one detail
    assert query_of(transport.urls[1])["priceTo"] == ["2300"]


def test_each_hotels_own_limit_still_decides_the_final_match(settings: Settings) -> None:
    body = page([rate("r1", "Hotel A", 2200), rate("r2", "Hotel B", 2250)], count=2)
    transport = FakeTransport([ROBOTS, ok(body), NO_DETAIL])
    entries = [entry("Hotel A", "sluga", cap="2000"), entry("Hotel B", "sluga", cap="2300")]

    offers = {
        o.hotel_name: o
        for o in provider(settings, transport).fetch_watchlist_offers(entries)
        if o.hotel_name
    }

    accept = ["itaka"]
    assert not matches(offers["Hotel A"], entries[0], accept, TODAY)  # 2200 > 2000
    assert matches(offers["Hotel B"], entries[1], accept, TODAY)
    assert not matches(offers["Hotel B"], entries[0], accept, TODAY)  # identities never mix


def test_two_slugs_make_two_queries_within_the_request_budget(settings: Settings) -> None:
    transport = FakeTransport(
        [
            ROBOTS,
            ok(page([rate("r1", "Hotel A", 2000)], count=1)),
            ok(page([rate("r2", "Hotel B", 2100)], count=1)),
            NO_DETAIL,
        ]
    )
    entries = [entry("Hotel A", "s1"), entry("Hotel B", "s2")]

    provider(settings, transport, max_requests=4).fetch_watchlist_offers(entries)

    assert [urlsplit(u).path for u in transport.urls[1:3]] == [
        "/wyniki-wyszukiwania/wakacje/s1/",
        "/wyniki-wyszukiwania/wakacje/s2/",
    ]
    assert len(transport.urls) == 4


def test_a_request_is_always_reserved_for_detail_confirmation(settings: Settings) -> None:
    transport = FakeTransport(
        [
            ROBOTS,
            ok(page([rate("r1", "Hotel A", 2000)], count=1)),
            ok(page([rate("r2", "Hotel B", 2100)], count=1)),
            NO_DETAIL,
        ]
    )
    entries = [entry("Hotel A", "s1"), entry("Hotel B", "s2"), entry("Hotel C", "s3")]

    provider(settings, transport, max_requests=4).fetch_watchlist_offers(entries)

    assert len(transport.urls) == 4  # robots + 2 pages; s3 skipped; detail kept
    assert "s3" not in " ".join(transport.urls)
    assert "r1" in transport.urls[-1]


def test_next_page_is_fetched_only_within_max_pages(settings: Settings) -> None:
    first = page([rate("r1", "Hotel A", 2000)], take=1, count=5)
    second = page([rate("r2", "Hotel A", 2050)], skip=1, take=1, count=5)
    transport = FakeTransport([ROBOTS, ok(first), ok(second)])

    offers = provider(
        settings, transport, max_pages=2, max_requests=4, max_detail_requests=0
    ).fetch_watchlist_offers([entry("Hotel A", "s1")])

    assert len(offers) == 2
    assert query_of(transport.urls[2])["page"] == ["2"]
    assert len(transport.urls) == 3  # max_pages=2 stops it despite count=5


def test_the_same_offer_in_two_queries_is_one_result(settings: Settings) -> None:
    same = page([rate("r1", "Hotel A", 2000)], count=1)
    transport = FakeTransport([ROBOTS, ok(same), ok(same), NO_DETAIL])
    entries = [entry("Hotel A", "s1"), entry("Hotel A Twin", "s2")]

    offers = provider(settings, transport, max_requests=4).fetch_watchlist_offers(entries)

    assert len(offers) == 1


def test_transient_timeout_on_a_page_keeps_what_was_gathered(settings: Settings) -> None:
    transport = FakeTransport(
        [ROBOTS, ok(page([rate("r1", "Hotel A", 2000)], count=1)), None, NO_DETAIL]
    )
    entries = [entry("Hotel A", "s1"), entry("Hotel B", "s2")]

    offers = provider(settings, transport, max_requests=4).fetch_watchlist_offers(entries)

    assert [o.hotel_name for o in offers] == ["Hotel A"]


def test_disallowed_path_fails_closed(settings: Settings) -> None:
    transport = FakeTransport([ok("User-agent: *\nDisallow: /wyniki-wyszukiwania/")])

    with pytest.raises(ValueError):
        provider(settings, transport).fetch_watchlist_offers([entry("Hotel A", "s1")])


def test_standard_fetch_is_unchanged_by_a_watchlist_destination(settings: Settings) -> None:
    transport = FakeTransport([ROBOTS, ok(page([rate("r1", "Hotel A", 1000)], count=1))])
    p = ItakaProvider(
        cast(ProviderConfig, {**settings.providers["itaka"], "max_detail_requests": 0}),
        settings.filters,
        transport,
        sleep=lambda _: None,
        today=lambda: TODAY,
        hotel_watchlist=[entry("Hotel A", "sluga")],
    )

    p.fetch()

    assert [urlsplit(u).path for u in transport.urls] == ["/robots.txt", "/last-minute/"]


# --- config --------------------------------------------------------------------


def test_itaka_slug_is_loaded_and_malformed_ones_disable_the_watchlist(
    write_hotel_watchlist: WriteHotelWatchlist, caplog: pytest.LogCaptureFixture
) -> None:
    good = write_hotel_watchlist([entry("A", "some-slug")])
    assert load_hotel_watchlist(good)[0]["provider_destinations"] == {"itaka": "some-slug"}

    bad = write_hotel_watchlist([entry("A", "Some Slug")])
    with caplog.at_level(logging.ERROR):
        assert load_hotel_watchlist(bad) == []
    assert "provider_destinations.itaka" in caplog.text
