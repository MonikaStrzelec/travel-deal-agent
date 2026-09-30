"""Destination-scoped TUI watchlist search (`provider_destinations["tui"]`).

Confirmed live 2026-09-30: a `c:<code>` token scopes the query to a destination
(e.g. `c:HRG`), which turns a 34-page global listing into typically one page.
Injected capture fakes; no network, no Playwright.
"""

import json
import logging
from datetime import date
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest
from test_tui_watchlist import watchlist_entry
from test_tui_watchlist_pagination import PagedCapture, make_provider, page_body, urls_of

from conftest import WriteHotelWatchlist
from travel_deal_agent.config import Settings, load_hotel_watchlist
from travel_deal_agent.config_types import HotelWatchlistEntry
from travel_deal_agent.providers.tui import BASE
from travel_deal_agent.providers.tui_query import (
    build_watchlist_search_path,
    split_watchlist_entries,
)
from travel_deal_agent.watchlist import matches, matches_any
from tui_support import raw_offer

TODAY = date(2026, 9, 30)


def tokens_of(path: str) -> list[str]:
    return parse_qs(urlsplit(path).query)["q"][0].split(":")


def destinations_in(path: str) -> list[str]:
    tokens = tokens_of(path)
    return [tokens[i + 1] for i, token in enumerate(tokens) if token == "c"]


def scoped(name: str, code: str, **overrides: object) -> HotelWatchlistEntry:
    return watchlist_entry(name=name, provider_destinations={"tui": code}, **overrides)


def plain(name: str, **overrides: object) -> HotelWatchlistEntry:
    return watchlist_entry(name=name, **overrides)


# --- query builder -------------------------------------------------------------


def test_scoped_entry_adds_destination_token() -> None:
    assert destinations_in(build_watchlist_search_path([scoped("A", "HRG")])) == ["HRG"]


def test_destinations_are_deduplicated_and_sorted_across_hotels() -> None:
    entries = [scoped("A", "SSH"), scoped("B", "HRG"), scoped("C", "SSH")]

    assert destinations_in(build_watchlist_search_path(entries)) == ["HRG", "SSH"]


def test_no_destination_keeps_the_unscoped_query() -> None:
    assert destinations_in(build_watchlist_search_path([plain("A")])) == []


def test_a_mixed_group_is_never_scoped() -> None:
    # A c: token is a hard filter; scoping a mixed group would hide the unscoped hotel.
    assert destinations_in(build_watchlist_search_path([scoped("A", "HRG"), plain("B")])) == []


def test_split_separates_scoped_from_unscoped_and_drops_unsupported_party_size() -> None:
    a, b = scoped("A", "HRG"), plain("B")
    c = scoped("C", "SSH", people=3)

    assert split_watchlist_entries([a, b, c]) == ([a], [b])


def test_destination_of_another_provider_is_ignored() -> None:
    entry = plain("A", provider_destinations={"itaka": "XYZ"})

    assert destinations_in(build_watchlist_search_path([entry])) == []


# --- provider ------------------------------------------------------------------


def test_scoped_watchlist_is_one_destination_query_not_a_global_listing(
    settings: Settings,
) -> None:
    capture = PagedCapture([page_body(1, 1, ["W1"])])
    entries = [scoped("Test Watchlist Hotel", "HRG")]

    offers = make_provider(settings, capture, watchlist_max_pages=3).fetch_watchlist_offers(entries)

    assert [o.offer_id for o in offers] == ["W1"]
    assert urls_of(capture) == [BASE + build_watchlist_search_path(entries)]
    assert destinations_in(urls_of(capture)[0]) == ["HRG"]


def test_fallback_without_destination_is_the_unscoped_paginated_query(
    settings: Settings,
) -> None:
    capture = PagedCapture([page_body(1, 30, ["W1"]), page_body(2, 30, ["W2"])])

    offers = make_provider(settings, capture, watchlist_max_pages=2).fetch_watchlist_offers(
        [plain("Test Watchlist Hotel")]
    )

    assert [o.offer_id for o in offers] == ["W1", "W2"]
    assert all(destinations_in(url) == [] for url in urls_of(capture))


def test_mixed_entries_run_a_scoped_and_an_unscoped_query(settings: Settings) -> None:
    capture = PagedCapture([page_body(1, 9, ["W1"]), page_body(1, 9, ["W2"])])
    entries = [scoped("Test Watchlist Hotel", "HRG"), plain("Z")]

    make_provider(settings, capture, watchlist_max_pages=2).fetch_watchlist_offers(entries)

    assert [destinations_in(url) for url in urls_of(capture)] == [["HRG"], []]


def test_extra_pages_go_to_the_scoped_group_first_within_the_total_budget(
    settings: Settings,
) -> None:
    capture = PagedCapture(
        [page_body(1, 9, ["W1"]), page_body(2, 9, ["W2"]), page_body(1, 9, ["W3"])]
    )
    entries = [scoped("Test Watchlist Hotel", "HRG"), plain("Z")]

    offers = make_provider(settings, capture, watchlist_max_pages=3).fetch_watchlist_offers(entries)

    assert [destinations_in(url) for url in urls_of(capture)] == [["HRG"], ["HRG"], []]
    assert [o.offer_id for o in offers] == ["W1", "W2", "W3"]


def test_listing_navigations_never_exceed_the_page_limit_with_enough_pages(
    settings: Settings,
) -> None:
    capture = PagedCapture([page_body(1, 9, ["W1"]), page_body(1, 9, ["W2"])])
    entries = [scoped("Test Watchlist Hotel", "HRG"), plain("Z")]

    make_provider(settings, capture, watchlist_max_pages=2).fetch_watchlist_offers(entries)

    assert len(capture.calls) == 2


def test_same_offer_returned_by_both_queries_is_one_result(settings: Settings) -> None:
    capture = PagedCapture([page_body(1, 1, ["W1"]), page_body(1, 1, ["W1"])])
    entries = [scoped("Test Watchlist Hotel", "HRG"), plain("Z")]

    offers = make_provider(settings, capture, watchlist_max_pages=2).fetch_watchlist_offers(entries)

    assert [o.offer_id for o in offers] == ["W1"]


def test_unsupported_party_size_makes_no_request(settings: Settings) -> None:
    capture = PagedCapture([])

    offers = make_provider(settings, capture).fetch_watchlist_offers([scoped("A", "HRG", people=3)])

    assert offers == []
    assert capture.calls == []


# --- several hotels: individual limits and identities --------------------------


def body_with(*offers: dict[str, object]) -> str:
    pagination = {"page": 0, "pageSize": 20, "totalResults": len(offers), "sorting": "price"}
    return json.dumps({"pagination": {**pagination, "pagesCount": 1}, "offers": list(offers)})


def priced(code: str, hotel: str, per_person: int) -> dict[str, object]:
    return raw_offer(
        code,
        hotelName=hotel,
        discountPerPersonPrice=str(per_person),
        originalPerPersonPrice=str(per_person),
        discountFullPrice=str(per_person * 2),
        originalFullPrice=str(per_person * 2),
        duration=7,
        returnDate="11.12.2026",
    )


def test_each_hotels_own_limit_and_identity_hold_with_a_shared_wider_query(
    settings: Settings,
) -> None:
    entries = [
        scoped("Hotel A", "HRG", max_price_per_person="2000", airports=["KTW"]),
        scoped("Hotel B", "SSH", max_price_per_person="2300", airports=["KTW"]),
        plain("Hotel C", max_price_per_person="1800", airports=["KTW"]),
    ]
    capture = PagedCapture(
        [
            body_with(
                priced("A1", "Hotel A", 2200),
                priced("B1", "Hotel B", 2250),
                priced("X1", "Hotel A Extra", 1000),
            ),
            body_with(priced("C1", "Hotel C", 1700)),
        ]
    )

    offers = {
        o.offer_id: o
        for o in make_provider(settings, capture, watchlist_max_pages=2).fetch_watchlist_offers(
            entries
        )
    }

    accept = ["tui"]
    assert "#4600" in tokens_of(urls_of(capture)[0])  # query reaches the widest limit
    assert not matches(offers["A1"], entries[0], accept, TODAY)  # 2200 > hotel A's 2000
    assert matches(offers["B1"], entries[1], accept, TODAY)
    assert matches(offers["C1"], entries[2], accept, TODAY)
    assert matches_any(offers["X1"], entries, accept, TODAY) is None  # no identity mixing


def test_each_groups_ceiling_uses_only_that_groups_widest_limit(settings: Settings) -> None:
    entries = [
        scoped("A", "HRG", max_price_per_person="2000"),
        plain("C", max_price_per_person="3000"),
    ]
    capture = PagedCapture([page_body(1, 1, []), page_body(1, 1, [])])

    make_provider(settings, capture, watchlist_max_pages=2).fetch_watchlist_offers(entries)

    scoped_tokens, unscoped_tokens = (tokens_of(url) for url in urls_of(capture))
    assert "#4000" in scoped_tokens
    assert "#6000" in unscoped_tokens


# --- config --------------------------------------------------------------------


def test_watchlist_without_the_new_field_still_loads(
    write_hotel_watchlist: WriteHotelWatchlist,
) -> None:
    path = write_hotel_watchlist([plain("A")])

    entries = load_hotel_watchlist(path)

    assert len(entries) == 1
    assert "provider_destinations" not in entries[0]


def test_destination_code_is_loaded(write_hotel_watchlist: WriteHotelWatchlist) -> None:
    path = write_hotel_watchlist([scoped("A", "HRG")])

    assert load_hotel_watchlist(path)[0]["provider_destinations"] == {"tui": "HRG"}


@pytest.mark.parametrize("bad", ["hrg", "", "H", "TOOLONGCODE", "HR G"])
def test_malformed_destination_code_disables_the_watchlist_with_an_error(
    write_hotel_watchlist: WriteHotelWatchlist, caplog: pytest.LogCaptureFixture, bad: str
) -> None:
    path = write_hotel_watchlist([cast(object, scoped("A", bad))])

    with caplog.at_level(logging.ERROR):
        assert load_hotel_watchlist(path) == []

    assert "provider_destinations" in caplog.text
