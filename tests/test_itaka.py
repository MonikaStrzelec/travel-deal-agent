"""Synthetic SSR fixtures and injected HTTP: these tests never contact ITAKA."""

import json
from dataclasses import replace
from decimal import Decimal
from urllib.parse import urlencode

import pytest

from travel_deal_agent.config import Settings
from travel_deal_agent.filtering import matches
from travel_deal_agent.models import Offer
from travel_deal_agent.providers.http import Response
from travel_deal_agent.providers.itaka import ItakaProvider
from travel_deal_agent.providers.itaka_data import normalize_rate, parse_page
from travel_deal_agent.providers.robots import robots_policy
from travel_deal_agent.storage import Store


class FlakyTransport:
    """Like FakeTransport, but a `None` entry raises a synthetic transient timeout."""

    def __init__(self, responses: list[Response | None]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, timeout: float) -> Response:
        assert timeout > 0
        self.urls.append(url)
        item = self.responses.pop(0)
        if item is None:
            raise TimeoutError("synthetic timeout")
        return item


@pytest.fixture
def raw() -> dict[str, object]:
    result: dict[str, object] = json.loads("""{
      "supplier":"itaka","supplierObjectId":"TEST","rateType":"holidays","currency":"PLN",
      "participantGroups":[{"price":280000,"rateId":"fixture-1","participants":[
        {"price":140000,"type":"adult","additionalPayments":[{"type":"TFG","amount":1000},{"type":"TFP","amount":1000}]},
        {"price":140000,"type":"adult","additionalPayments":[{"type":"TFG","amount":1000},{"type":"TFP","amount":1000}]}]}],
      "duration":{"days":8},"segments":[
        {"type":"flight","beginDate":"2030-10-01","endDate":"2030-10-01","departure":{"title":"Łódź"},"destination":{"title":"Burgas"}},
        {"type":"hotel","beginDate":"2030-10-01","endDate":"2030-10-08","participantGroups":[{"meal":{"id":"A","title":"All inclusive"},"room":{"id":"DBL","title":"Double"},"baseRoomCode":"D2"}],
         "content":{"title":"Synthetic Hotel","hotelRating":40,"reviews":{"customersRating":5.4,"reviewsNumber":123},"geographicalIdentifiers":[{"id":"bulgaria","title":"Bułgaria","type":"country"}]}},
        {"type":"flight","beginDate":"2030-10-08","endDate":"2030-10-09","departure":{"title":"Burgas"},"destination":{"title":"Łódź"}}]}
    """)
    return result


def html(rates: list[dict[str, object]], skip: int = 0, count: int = 1) -> str:
    data = {
        "props": {
            "pageProps": {
                "initialQueryState": {
                    "queries": [
                        {
                            "queryKey": ["rates", {"skip": skip, "take": 1}],
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
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + "</script>"


class FakeTransport:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, timeout: float) -> Response:
        assert timeout > 0
        self.urls.append(url)
        return self.responses.pop(0)


def response(text: str, status: int = 200) -> Response:
    return Response(status, text, {})


def test_normalization_and_price_gate(
    raw: dict[str, object], settings: Settings, store: Store
) -> None:
    # Arrange / Act
    offer = normalize_rate(raw, [])
    # Assert
    assert (offer.country, offer.departure_airport, offer.board_type) == ("BG", "LCJ", "AI")
    assert (offer.hotel_stars, offer.rating, offer.number_of_reviews) == (4, 5.4, 123)
    assert str(offer.return_date) == "2030-10-09"
    assert offer.price_per_person == Decimal("1420")
    assert offer.total_price == Decimal("2840")
    assert not offer.price_is_complete
    assert not matches(offer, settings.filters)
    # Store.observe trusts its `eligible` argument (see its docstring); the real
    # pipeline only ever passes what matches() decided, which is False here.
    assert store.observe(offer, False) == []
    assert store.pending() == []
    assert store.get_offer("itaka", offer.offer_id) is not None


def test_poznan_departure_airport_mapping(raw: dict[str, object]) -> None:
    # Arrange: tests/fixtures/itaka/FUERIOC_mapping.json (real detail
    # reconnaissance) ties title "Poznań" to IATA code "POZ" -- see
    # itaka_data.AIRPORTS.
    changed = json.loads(json.dumps(raw, ensure_ascii=False).replace("Łódź", "Poznań"))
    offer = normalize_rate(changed, [])
    assert offer.departure_airport == "POZ"


def test_identity_excludes_price_but_includes_variant(raw: dict[str, object]) -> None:
    original = normalize_rate(raw, [])
    repriced = json.loads(json.dumps(raw).replace("140000", "130000").replace("280000", "260000"))
    assert normalize_rate(repriced, []).offer_id == original.offer_id
    for before, after in [
        ("fixture-1", "fixture-2"),
        ("D2", "D3"),
        ("2030-10-09", "2030-10-10"),
        ("Double", "Suite"),
        ("All inclusive", "Breakfast"),
    ]:
        changed = json.loads(json.dumps(raw).replace(before, after))
        assert normalize_rate(changed, []).offer_id != original.offer_id


def test_pagination(raw: dict[str, object], settings: Settings) -> None:
    second = json.loads(json.dumps(raw).replace("fixture-1", "fixture-2"))
    transport = FakeTransport(
        [
            response("User-agent: *\nDisallow: /api*"),
            response(html([raw], count=2)),
            response(html([second], skip=1, count=2)),
        ]
    )
    provider = ItakaProvider(
        settings.providers["itaka"], settings.filters, transport, sleep=lambda _: None
    )
    offers = provider.fetch()
    assert len(offers) == 2
    assert transport.urls == [
        "https://www.itaka.pl/robots.txt",
        "https://www.itaka.pl/last-minute/",
        "https://www.itaka.pl/last-minute/?page=2",
    ]


def test_transient_network_error_on_later_page_keeps_earlier_offers(
    raw: dict[str, object], settings: Settings
) -> None:
    # Arrange: page 1 succeeds (1 of 2 total offers); page 2 hits a transient
    # network error. Matches wakacje.py's documented policy: no retry, but the
    # already-parsed page-1 offer is not discarded.
    transport = FlakyTransport(
        [response("User-agent: *\nDisallow: /api*"), response(html([raw], count=2)), None]
    )
    provider = ItakaProvider(
        settings.providers["itaka"], settings.filters, transport, sleep=lambda _: None
    )
    offers = provider.fetch()
    assert len(offers) == 1
    assert len(transport.urls) == 3


def test_transient_network_error_on_detail_request_keeps_listing_only_offer(
    raw: dict[str, object], settings: Settings
) -> None:
    # Arrange: the one listing page resolves a detail-eligible candidate, but
    # the detail request itself hits a transient network error. The listing
    # offer must survive (unconfirmed), and the whole cycle must not abort.
    link = "/wczasy/test,fixture-1/?" + urlencode({"id[0]": "fixture-1"})
    data = {
        "props": {
            "pageProps": {
                "initialQueryState": {
                    "queries": [
                        {
                            "queryKey": ["rates", {"skip": 0, "take": 1}],
                            "state": {
                                "data": {
                                    "main": {"multiRoomRates": {"list": [raw], "ratesCount": 1}}
                                }
                            },
                        }
                    ]
                }
            }
        }
    }
    body = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(data)
        + f'</script><a href="{link}">Offer</a>'
    )
    transport = FlakyTransport([response("User-agent: *\nDisallow: /api*"), response(body), None])
    provider = ItakaProvider(
        settings.providers["itaka"], settings.filters, transport, sleep=lambda _: None
    )
    offers = provider.fetch()
    assert len(offers) == 1
    assert not offers[0].price_is_complete
    assert len(transport.urls) == 3


def _link_page(rates: list[dict[str, object]], links: dict[str, str], take: int) -> str:
    """A listing page with one <a href> detail link per rate, keyed by rateId."""
    data = {
        "props": {
            "pageProps": {
                "initialQueryState": {
                    "queries": [
                        {
                            "queryKey": ["rates", {"skip": 0, "take": take}],
                            "state": {
                                "data": {
                                    "main": {
                                        "multiRoomRates": {"list": rates, "ratesCount": len(rates)}
                                    }
                                }
                            },
                        }
                    ]
                }
            }
        }
    }
    anchors = "".join(f'<a href="{href}">{rate_id}</a>' for rate_id, href in links.items())
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(data)
        + "</script>"
        + anchors
    )


def _rate_link(rate_id: str) -> str:
    return "/wczasy/test," + rate_id + "/?" + urlencode({"id[0]": rate_id})


def test_no_detail_request_when_every_candidate_is_ineligible(
    raw: dict[str, object], settings: Settings
) -> None:
    # Arrange: the only candidate on the page has an unmapped departure
    # airport, so it can never pass filtering.matches_criteria. No detail
    # request should be attempted at all -- not even a "best effort" one.
    ineligible = json.loads(json.dumps(raw))
    ineligible["segments"][0]["departure"]["title"] = "Nieznane Miasto"
    body = _link_page([ineligible], {"fixture-1": _rate_link("fixture-1")}, take=1)
    transport = FakeTransport([response("User-agent: *\nDisallow: /api*"), response(body)])
    provider = ItakaProvider(
        settings.providers["itaka"], settings.filters, transport, sleep=lambda _: None
    )
    offers = provider.fetch()
    # Assert: no detail request (2 requests total: robots + listing), and the
    # listing-only offer stays unconfirmed.
    assert len(transport.urls) == 2
    assert len(offers) == 1
    assert offers[0].price_is_complete is False


def test_several_eligible_candidates_keep_listing_order_and_the_detail_limit(
    raw: dict[str, object], settings: Settings
) -> None:
    # Arrange: two candidates on one page, BOTH eligible from listing data
    # alone. With the default max_detail_requests=1, the request must go to
    # whichever is first in the listing's own order (fixture-1), never both,
    # and the order must not be disturbed just because a second one also
    # qualifies.
    second = json.loads(json.dumps(raw).replace("fixture-1", "fixture-2"))
    body = _link_page(
        [raw, second],
        {"fixture-1": _rate_link("fixture-1"), "fixture-2": _rate_link("fixture-2")},
        take=2,
    )
    transport = FakeTransport(
        [
            response("User-agent: *\nDisallow: /api*"),
            response(body),
            response("<html>no detail data</html>"),
        ]
    )
    provider = ItakaProvider(
        settings.providers["itaka"], settings.filters, transport, sleep=lambda _: None
    )
    provider.fetch()
    # Assert: exactly one detail request, for the first-listed candidate.
    assert len(transport.urls) == 3
    assert "fixture-1" in transport.urls[-1]
    assert "fixture-2" not in transport.urls[-1]


def test_pagination_continues_when_page_one_has_no_detail_candidate(
    raw: dict[str, object], settings: Settings
) -> None:
    # Arrange: page 1's only candidate is ineligible (no detail request should
    # be spent on it); pagination must still continue to page 2 exactly as
    # before, and page 2's candidate is kept as a normal listing-only offer.
    ineligible = json.loads(json.dumps(raw))
    ineligible["segments"][0]["departure"]["title"] = "Nieznane Miasto"
    second = json.loads(json.dumps(raw).replace("fixture-1", "fixture-2"))
    transport = FakeTransport(
        [
            response("User-agent: *\nDisallow: /api*"),
            response(html([ineligible], count=2)),
            response(html([second], skip=1, count=2)),
        ]
    )
    provider = ItakaProvider(
        settings.providers["itaka"], settings.filters, transport, sleep=lambda _: None
    )
    offers = provider.fetch()
    # Assert: both pages fetched, no detail request anywhere.
    assert len(offers) == 2
    assert transport.urls == [
        "https://www.itaka.pl/robots.txt",
        "https://www.itaka.pl/last-minute/",
        "https://www.itaka.pl/last-minute/?page=2",
    ]
    assert all(not o.price_is_complete for o in offers)


def test_shortlist_prefers_the_eligible_candidate_for_the_scarce_detail_request(
    raw: dict[str, object], settings: Settings
) -> None:
    # Arrange: two listing candidates on one page. The first (listed first, so
    # it would have won under plain listing-order selection) has an unmapped
    # departure airport and can never pass filtering.matches_criteria, so it is
    # now excluded from the shortlist entirely (not merely deprioritized); the
    # second is otherwise identical but has a real, allowed airport. With only
    # one detail request available (max_detail_requests=1), it must go to the
    # eligible candidate, not the one listed first.
    ineligible = json.loads(json.dumps(raw).replace("fixture-1", "fixture-2"))
    ineligible["segments"][0]["departure"]["title"] = "Nieznane Miasto"

    def rate_link(rate_id: str) -> str:
        return "/wczasy/test," + rate_id + "/?" + urlencode({"id[0]": rate_id})

    data = {
        "props": {
            "pageProps": {
                "initialQueryState": {
                    "queries": [
                        {
                            "queryKey": ["rates", {"skip": 0, "take": 2}],
                            "state": {
                                "data": {
                                    "main": {
                                        "multiRoomRates": {
                                            "list": [ineligible, raw],
                                            "ratesCount": 2,
                                        }
                                    }
                                }
                            },
                        }
                    ]
                }
            }
        }
    }
    body = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(data)
        + "</script>"
        + f'<a href="{rate_link("fixture-2")}">A</a>'
        + f'<a href="{rate_link("fixture-1")}">B</a>'
    )
    transport = FakeTransport(
        [
            response("User-agent: *\nDisallow: /api*"),
            response(body),
            response("<html>no detail data</html>"),
        ]
    )
    provider = ItakaProvider(
        settings.providers["itaka"], settings.filters, transport, sleep=lambda _: None
    )
    # Act
    provider.fetch()
    # Assert: the one detail request went to the eligible candidate
    # (fixture-1), not the ineligible one listed first (fixture-2).
    assert len(transport.urls) == 3
    assert "fixture-1" in transport.urls[-1]
    assert "fixture-2" not in transport.urls[-1]


@pytest.mark.parametrize("status", [301, 403, 429, 503])
def test_no_retry_or_redirect(status: int, settings: Settings) -> None:
    transport = FakeTransport([response("", status)])
    with pytest.raises(ValueError, match="HTTP"):
        ItakaProvider(settings.providers["itaka"], settings.filters, transport).fetch()
    assert len(transport.urls) == 1


@pytest.mark.parametrize(
    "robots",
    ["<html>CAPTCHA</html>", "User-agent: *\nDisallow: /last*", "User-agent: Other\nDisallow: /"],
)
def test_robots_fail_closed(robots: str, settings: Settings) -> None:
    transport = FakeTransport([response(robots)])
    with pytest.raises(ValueError):
        ItakaProvider(settings.providers["itaka"], settings.filters, transport).fetch()
    assert len(transport.urls) == 1


def test_repeated_page_rejected(raw: dict[str, object], settings: Settings) -> None:
    transport = FakeTransport(
        [
            response("User-agent: *\nDisallow: /api"),
            response(html([raw], count=2)),
            response(html([raw], count=2)),
        ]
    )
    with pytest.raises(ValueError, match="pagination"):
        ItakaProvider(
            settings.providers["itaka"], settings.filters, transport, sleep=lambda _: None
        ).fetch()


def test_schema_change_fails_closed() -> None:
    with pytest.raises(ValueError):
        parse_page("<html>Challenge</html>")
    assert robots_policy("User-agent: *\nDisallow: /api*\nCrawl-delay: 10", "/last-minute/") == 10


def test_unverified_price_cannot_pass_even_if_cheap(offer: Offer, settings: Settings) -> None:
    assert not matches(
        replace(offer, price_is_complete=False, price_per_person=Decimal("1")), settings.filters
    )


@pytest.mark.parametrize(
    "before,after",
    [
        ('"price":280000', '"price":280001'),
        ('"additionalPayments"', '"missingPayments"'),
        ('"TFG"', '"UNKNOWN"'),
        ('"TFG"', '"TFP"'),
    ],
)
def test_uncertain_fees_remove_price(raw: dict[str, object], before: str, after: str) -> None:
    changed = json.loads(json.dumps(raw, separators=(",", ":")).replace(before, after))
    candidate = normalize_rate(changed, [])
    assert candidate.price_per_person is None
    assert candidate.total_price is None
    assert not candidate.price_is_complete


@pytest.mark.parametrize("maximum,request_limit,expected", [(1, 4, 1), (None, 4, 3), (None, 2, 1)])
def test_configurable_page_and_request_budgets(
    raw: dict[str, object],
    settings: Settings,
    maximum: int | None,
    request_limit: int,
    expected: int,
) -> None:
    cfg = settings.providers["itaka"].copy()
    cfg["max_pages"] = maximum
    cfg["max_requests"] = request_limit
    pages = [
        html([json.loads(json.dumps(raw).replace("fixture-1", f"fixture-{i}"))], skip=i, count=3)
        for i in range(3)
    ]
    transport = FakeTransport(
        [response("User-agent: *\nDisallow: /api")] + [response(p) for p in pages]
    )
    offers = ItakaProvider(cfg, settings.filters, transport, sleep=lambda _: None).fetch()
    assert len(offers) == expected
    assert len(transport.urls) == expected + 1


def test_timeout_propagates_without_retry(settings: Settings) -> None:
    class TimeoutTransport:
        def get(self, url: str, timeout: float) -> Response:
            raise TimeoutError("synthetic timeout")

    with pytest.raises(TimeoutError):
        ItakaProvider(settings.providers["itaka"], settings.filters, TimeoutTransport()).fetch()


def test_unknown_source_fields_remain_unknown(raw: dict[str, object]) -> None:
    changed = json.loads(
        json.dumps(raw)
        .replace("bulgaria", "unmapped")
        .replace("40", "45")
        .replace("5.4", "6.5")
        .replace("All inclusive", "Ambiguous")
    )
    offer = normalize_rate(changed, [])
    assert offer.country is None
    assert offer.hotel_stars is None
    assert offer.rating is None
    assert offer.board_type is None


def test_unmodeled_flight_number_changes_identity(raw: dict[str, object]) -> None:
    original = normalize_rate(raw, [])
    changed = json.loads(json.dumps(raw))
    changed["segments"][0]["flightNumber"] = "SYN123"
    assert normalize_rate(changed, []).offer_id != original.offer_id


def test_hotel_review_update_preserves_identity(raw: dict[str, object]) -> None:
    original = normalize_rate(raw, [])
    changed = json.loads(json.dumps(raw))
    changed["segments"][1]["content"]["reviews"]["reviewsNumber"] = 124
    assert normalize_rate(changed, []).offer_id == original.offer_id
