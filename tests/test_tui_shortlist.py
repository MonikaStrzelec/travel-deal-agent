"""Offline tests for the TUI detail-confirmation candidate shortlist.

`TuiProvider._shortlist` picks which offer(s) get the scarce `max_detail_requests`
real-time price check: the cheapest offers that already pass every
listing-known filter (`filtering.matches_criteria`), never simply the first
offer(s) in TUI's own listing order. No network, no Playwright -- capture/
capture_price are injected fakes throughout.
"""

import json
from datetime import datetime, timezone

from travel_deal_agent.config import Settings
from travel_deal_agent.config_types import ProviderConfig
from travel_deal_agent.providers.http import Response
from travel_deal_agent.providers.tui import TuiProvider

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
CONFIG: ProviderConfig = {
    "enabled": True,
    "interval_seconds": 3600,
    "max_pages": 1,
    "max_detail_requests": 1,
}
NOT_AVAILABLE_BODY = json.dumps({"offerStatus": "NOT_AVAILABLE", "alternativeOffers": []})


def raw_offer(offer_code: str, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "hotelCode": offer_code[:8],
        "hotelName": f"Hotel {offer_code}",
        "hotelStandard": 4.0,
        "offerCode": offer_code,
        "duration": 6,
        "offerUrl": f"/wypoczynek/turcja/hotel-{offer_code.lower()}/OfferCodeWS/{offer_code}",
        "breadcrumbs": [{"label": "Turcja"}],
        "discountFullPrice": "2000",
        "originalFullPrice": "2000",
        "discountPerPersonPrice": "1000",
        "originalPerPersonPrice": "1000",
        "departureDate": "04.12.2026",
        "returnDate": "10.12.2026",
        "departureTime": "15:30",
        "departureAirport": "Katowice",
        "boardType": "Trzy posiłki",
        "boardCode": "GT06-FB",
        "tripAdvisorRating": 4.3,
        "tripAdvisorReviewsNo": 777,
        "participants": "2 Dorosłych + 0 Dzieci",
        "currency": "PLN",
        "soldOut": False,
    }
    base.update(overrides)
    return base


def listing_body(offers: list[dict[str, object]]) -> str:
    return json.dumps(
        {
            "pagination": {
                "page": 0,
                "pageSize": 20,
                "totalResults": len(offers),
                "sorting": "price",
                "pagesCount": 1,
            },
            "offers": offers,
            "responseType": "NORMAL",
            "currency": "PLN",
        }
    )


def charter_realtime_body(offer_code: str, total_price: int) -> str:
    return json.dumps(
        {
            "offerStatus": "AVAILABLE",
            "offerCode": offer_code,
            "priceDetails": {
                "totalPrice": total_price,
                "totalDiscountPrice": total_price,
                "priceGuaranteeFund": 60,
                "priceGuaranteeFundInfo": "Turystyczny Fundusz Gwarancyjny",
                "priceDifference": 0,
                "currency": "PLN",
                "pricePerPerson": total_price // 2,
                "factor": 1.0,
                "discountPercentage": 0,
            },
            "travellerCount": {"adults": 2, "children": 0},
            "outboundFlight": {"departureAirportCode": "KTW"},
            "accommodations": [{"hotelCode": offer_code[:8], "duration": 6}],
            "startDate": "2026-12-04T15:30:00",
            "endDate": "2026-12-10T00:00:00",
            "tags": ["CHARTER_FLIGHT"],
            "offerTravelType": "BYPLANE",
            "analyticsData": {"values": {"flight_type": "CHART"}},
        }
    )


class FakeTransport:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = responses

    def get(self, url: str, timeout: float) -> Response:
        return self.responses.pop(0)


def robots_response() -> Response:
    return Response(200, "User-agent: *\nDisallow: /api/", {})


class FakeCapture:
    """Returns bodies[i] for the i-th call; records every requested URL."""

    def __init__(self, bodies: list[str]) -> None:
        self.bodies = list(bodies)
        self.calls: list[str] = []

    def __call__(self, url: str, timeout_seconds: float) -> str:
        self.calls.append(url)
        return self.bodies.pop(0)


def make_provider(
    settings: Settings, capture: FakeCapture, capture_price: FakeCapture, **cfg: object
) -> TuiProvider:
    configuration: ProviderConfig = {**CONFIG, **cfg}  # type: ignore[typeddict-item]
    return TuiProvider(
        configuration,
        settings.filters,
        FakeTransport([robots_response()]),
        capture,
        capture_price,
        sleep=lambda _: None,
        wall_clock=lambda: NOW,
    )


# --- 1: a too-expensive first offer is skipped in favor of an eligible second ----


def test_first_offer_too_expensive_detail_goes_to_second(settings: Settings) -> None:
    offers = [
        raw_offer("EXPEN1", discountPerPersonPrice="1600", discountFullPrice="3200"),
        raw_offer("CHEAP2", discountPerPersonPrice="1000"),
    ]
    capture = FakeCapture([listing_body(offers)])
    capture_price = FakeCapture([NOT_AVAILABLE_BODY])
    make_provider(settings, capture, capture_price).fetch()
    assert len(capture_price.calls) == 1
    assert "CHEAP2" in capture_price.calls[0]


# --- 2: a disallowed-board first offer is skipped in favor of an eligible second -


def test_first_offer_wrong_board_detail_goes_to_second(settings: Settings) -> None:
    offers = [
        raw_offer("WRONGBOARD1", boardType="Bez wyżywienia", boardCode="GT06-AO"),
        raw_offer("RIGHTBOARD2", boardType="Trzy posiłki", boardCode="GT06-FB"),
    ]
    capture = FakeCapture([listing_body(offers)])
    capture_price = FakeCapture([NOT_AVAILABLE_BODY])
    make_provider(settings, capture, capture_price).fetch()
    assert len(capture_price.calls) == 1
    assert "RIGHTBOARD2" in capture_price.calls[0]


# --- 3: among several eligible candidates, the cheapest is chosen ----------------


def test_cheapest_eligible_candidate_is_chosen(settings: Settings) -> None:
    offers = [
        raw_offer("MID3", discountPerPersonPrice="1200"),
        raw_offer("CHEAPEST3", discountPerPersonPrice="900"),
        raw_offer("EXPENSIVE3", discountPerPersonPrice="1400"),
    ]
    capture = FakeCapture([listing_body(offers)])
    capture_price = FakeCapture([NOT_AVAILABLE_BODY])
    make_provider(settings, capture, capture_price).fetch()
    assert len(capture_price.calls) == 1
    assert "CHEAPEST3" in capture_price.calls[0]


# --- 4: no potentially eligible candidate at all -> zero detail requests --------


def test_no_eligible_candidate_makes_zero_detail_requests(settings: Settings) -> None:
    offers = [
        raw_offer("TOOEXPENSIVE4", discountPerPersonPrice="1600", discountFullPrice="3200"),
        raw_offer("WRONGBOARD4", boardType="Bez wyżywienia", boardCode="GT06-AO"),
    ]
    capture = FakeCapture([listing_body(offers)])
    capture_price = FakeCapture([])
    result = make_provider(settings, capture, capture_price).fetch()
    assert capture_price.calls == []
    # Both offers are still returned, just never confirmed.
    assert {o.offer_id for o in result} == {"TOOEXPENSIVE4", "WRONGBOARD4"}
    assert all(o.price_is_complete is False for o in result)


# --- 5: an incomplete-price candidate still enters the shortlist ----------------


def test_incomplete_price_candidate_still_enters_shortlist(settings: Settings) -> None:
    # Every TUI listing offer starts with price_is_complete=False; this is
    # exactly what the detail request exists to try to confirm, so it must
    # never disqualify a candidate from the shortlist itself.
    offers = [raw_offer("ONLYCANDIDATE5")]
    capture = FakeCapture([listing_body(offers)])
    capture_price = FakeCapture([NOT_AVAILABLE_BODY])
    result = make_provider(settings, capture, capture_price).fetch()
    assert len(capture_price.calls) == 1
    assert "ONLYCANDIDATE5" in capture_price.calls[0]
    assert result[0].price_is_complete is False  # NOT_AVAILABLE stays unconfirmed


# --- 6: AVAILABLE + correct mandatory fees confirms price_is_complete=True ------


def test_available_confirmation_sets_price_is_complete_true(settings: Settings) -> None:
    offers = [raw_offer("CONFIRMME6", discountPerPersonPrice="1000", discountFullPrice="2000")]
    capture = FakeCapture([listing_body(offers)])
    capture_price = FakeCapture([charter_realtime_body("CONFIRMME6", total_price=2000)])
    result = make_provider(settings, capture, capture_price).fetch()
    assert result[0].price_is_complete is True
    assert result[0].price_per_person == 1030  # (2000 + 60 TFG/TFP) / 2


# --- 7: NOT_AVAILABLE stays unconfirmed, ineligible, no alert path --------------


def test_not_available_stays_incomplete_and_ineligible(settings: Settings) -> None:
    from travel_deal_agent.filtering import matches

    offers = [raw_offer("GONE7")]
    capture = FakeCapture([listing_body(offers)])
    capture_price = FakeCapture([NOT_AVAILABLE_BODY])
    result = make_provider(settings, capture, capture_price).fetch()
    offer = result[0]
    assert offer.price_is_complete is False
    assert offer.price_verification_reason is not None
    assert "NOT_AVAILABLE" in offer.price_verification_reason
    # STRICT policy (TUI not in accept_incomplete_price_from): never eligible.
    assert matches(offer, settings.filters) is False


# --- 8: max_detail_requests=1 is respected even with many eligible candidates ---


def test_max_detail_requests_one_is_respected_with_many_candidates(settings: Settings) -> None:
    offers = [raw_offer(f"MANY{i}", discountPerPersonPrice=str(1000 + i)) for i in range(5)]
    capture = FakeCapture([listing_body(offers)])
    capture_price = FakeCapture([NOT_AVAILABLE_BODY])
    make_provider(settings, capture, capture_price, max_detail_requests=1).fetch()
    assert len(capture_price.calls) == 1
    assert "MANY0" in capture_price.calls[0]  # cheapest of the five
