"""Synthetic SSR fixtures and injected HTTP: these tests never contact ITAKA."""

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from travel_deal_agent.config import Settings
from travel_deal_agent.filtering import matches
from travel_deal_agent.models import Offer
from travel_deal_agent.providers.http import Response
from travel_deal_agent.providers.itaka import ItakaProvider, robots_policy
from travel_deal_agent.providers.itaka_data import normalize_rate, parse_page
from travel_deal_agent.storage import Store


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


def test_identity_excludes_price_but_includes_variant(raw: dict[str, object]) -> None:
    # Arrange
    original = normalize_rate(raw, [])
    repriced = json.loads(json.dumps(raw).replace("140000", "130000").replace("280000", "260000"))
    # Act / Assert
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
    # Arrange
    second = json.loads(json.dumps(raw).replace("fixture-1", "fixture-2"))
    transport = FakeTransport(
        [
            response("User-agent: *\nDisallow: /api*"),
            response(html([raw], count=2)),
            response(html([second], skip=1, count=2)),
        ]
    )
    provider = ItakaProvider(settings.providers["itaka"], transport, sleep=lambda _: None)
    # Act
    offers = provider.fetch()
    # Assert
    assert len(offers) == 2
    assert transport.urls == [
        "https://www.itaka.pl/robots.txt",
        "https://www.itaka.pl/last-minute/",
        "https://www.itaka.pl/last-minute/?page=2",
    ]


@pytest.mark.parametrize("status", [301, 403, 429, 503])
def test_no_retry_or_redirect(status: int, settings: Settings) -> None:
    # Arrange
    transport = FakeTransport([response("", status)])
    # Act / Assert
    with pytest.raises(ValueError, match="HTTP"):
        ItakaProvider(settings.providers["itaka"], transport).fetch()
    assert len(transport.urls) == 1


@pytest.mark.parametrize(
    "robots",
    ["<html>CAPTCHA</html>", "User-agent: *\nDisallow: /last*", "User-agent: Other\nDisallow: /"],
)
def test_robots_fail_closed(robots: str, settings: Settings) -> None:
    # Arrange
    transport = FakeTransport([response(robots)])
    # Act / Assert
    with pytest.raises(ValueError):
        ItakaProvider(settings.providers["itaka"], transport).fetch()
    assert len(transport.urls) == 1


def test_repeated_page_rejected(raw: dict[str, object], settings: Settings) -> None:
    # Arrange
    transport = FakeTransport(
        [
            response("User-agent: *\nDisallow: /api"),
            response(html([raw], count=2)),
            response(html([raw], count=2)),
        ]
    )
    # Act / Assert
    with pytest.raises(ValueError, match="pagination"):
        ItakaProvider(settings.providers["itaka"], transport, sleep=lambda _: None).fetch()


def test_schema_change_fails_closed() -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValueError):
        parse_page("<html>Challenge</html>")
    assert robots_policy("User-agent: *\nDisallow: /api*\nCrawl-delay: 10", "/last-minute/") == 10


def test_unverified_price_cannot_pass_even_if_cheap(offer: Offer, settings: Settings) -> None:
    # Arrange / Act / Assert
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
    # Arrange
    changed = json.loads(json.dumps(raw, separators=(",", ":")).replace(before, after))
    # Act
    candidate = normalize_rate(changed, [])
    # Assert
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
    # Arrange
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
    # Act
    offers = ItakaProvider(cfg, transport, sleep=lambda _: None).fetch()
    # Assert
    assert len(offers) == expected
    assert len(transport.urls) == expected + 1


def test_request_spacing_and_deadline() -> None:
    from travel_deal_agent.providers.http import RequestBudget

    # Arrange
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    transport = FakeTransport([response("a"), response("b")])
    budget = RequestBudget(transport, 3, 15, 9, 5, lambda: now[0], sleep)
    # Act
    budget.get("https://example.invalid/1")
    budget.get("https://example.invalid/2")
    # Assert
    assert now[0] == 5
    with pytest.raises(ValueError, match="deadline"):
        budget.get("https://example.invalid/3")
    assert len(transport.urls) == 2


def test_timeout_propagates_without_retry(settings: Settings) -> None:
    class TimeoutTransport:
        def get(self, url: str, timeout: float) -> Response:
            raise TimeoutError("synthetic timeout")

    # Arrange / Act / Assert
    with pytest.raises(TimeoutError):
        ItakaProvider(settings.providers["itaka"], TimeoutTransport()).fetch()


def test_unknown_source_fields_remain_unknown(raw: dict[str, object]) -> None:
    # Arrange
    changed = json.loads(
        json.dumps(raw)
        .replace("bulgaria", "unmapped")
        .replace("40", "45")
        .replace("5.4", "6.5")
        .replace("All inclusive", "Ambiguous")
    )
    # Act
    offer = normalize_rate(changed, [])
    # Assert
    assert offer.country is None
    assert offer.hotel_stars is None
    assert offer.rating is None
    assert offer.board_type is None


def test_unmodeled_flight_number_changes_identity(raw: dict[str, object]) -> None:
    # Arrange
    original = normalize_rate(raw, [])
    changed = json.loads(json.dumps(raw))
    changed["segments"][0]["flightNumber"] = "SYN123"
    # Act / Assert
    assert normalize_rate(changed, []).offer_id != original.offer_id


def test_hotel_review_update_preserves_identity(raw: dict[str, object]) -> None:
    # Arrange
    original = normalize_rate(raw, [])
    changed = json.loads(json.dumps(raw))
    changed["segments"][1]["content"]["reviews"]["reviewsNumber"] = 124
    # Act / Assert
    assert normalize_rate(changed, []).offer_id == original.offer_id
