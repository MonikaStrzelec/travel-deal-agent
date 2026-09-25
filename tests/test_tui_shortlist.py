"""Offline tests for the TUI detail-confirmation candidate shortlist.

`TuiProvider._shortlist` picks which offer(s) get the scarce `max_detail_requests`
real-time price check. Only offers that already pass every listing-known
filter (`filtering.matches_criteria`) are eligible at all; among those, the
candidate is chosen by listing-time `attractiveness.classify_offer` category
(HOT before GOOD before MATCH -- the existing V0 classification, never a new
score) and only within the same category by ascending `price_per_person` --
never simply the cheapest offer that clears the eligibility bar. No network,
no Playwright -- capture/capture_price are injected fakes throughout.
"""

import json
from datetime import datetime, timezone

from travel_deal_agent.config import Settings
from travel_deal_agent.config_types import ProviderConfig
from travel_deal_agent.providers.tui import TuiProvider
from tui_support import NOT_AVAILABLE_BODY, FakeTransport, raw_offer, robots_response

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
CONFIG: ProviderConfig = {
    "enabled": True,
    "interval_seconds": 3600,
    "max_pages": 1,
    "max_detail_requests": 1,
}


def category_offer(offer_code: str, price: int, rating: float) -> dict[str, object]:
    """A raw offer with `hotelStandard` fixed at 3.0 (below every star-bump
    threshold in `attractiveness.classify_hotel_quality`), so its
    HOT/GOOD/MATCH category is controlled purely by `price` (VALUE, via the
    fixed 6-night stay below) and `rating` (HOTEL QUALITY, via the tui native
    1-5 scale in `tests/fixtures/test_config.json`) -- never nudged by stars.
    `departureAirport`/`boardType` stay at their neutral/normal defaults, so
    AIRPORT and BOARD never contribute a "strong" or "weak" level either.
    """
    return raw_offer(
        offer_code,
        discountPerPersonPrice=str(price),
        discountFullPrice=str(price * 2),
        tripAdvisorRating=rating,
        hotelStandard=3.0,
    )


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
        attractiveness=settings.attractiveness,
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


# --- 3: a cheaper MATCH offer loses to a pricier GOOD offer ----------------------


def test_cheaper_match_loses_to_pricier_good(settings: Settings) -> None:
    offers = [
        category_offer("CHEAPMATCH3", price=1100, rating=4.1),  # normal + normal -> MATCH
        category_offer("PRICIERGOOD3", price=1200, rating=4.5),  # normal + strong -> GOOD
    ]
    capture = FakeCapture([listing_body(offers)])
    capture_price = FakeCapture([NOT_AVAILABLE_BODY])
    make_provider(settings, capture, capture_price).fetch()
    assert len(capture_price.calls) == 1
    assert "PRICIERGOOD3" in capture_price.calls[0]


# --- 4: a cheaper GOOD offer loses to a pricier HOT offer ------------------------


def test_cheaper_good_loses_to_pricier_hot(settings: Settings) -> None:
    offers = [
        category_offer("CHEAPGOOD4", price=500, rating=3.0),  # strong + weak -> GOOD
        category_offer("PRICIERHOT4", price=1000, rating=4.5),  # strong + strong -> HOT
    ]
    capture = FakeCapture([listing_body(offers)])
    capture_price = FakeCapture([NOT_AVAILABLE_BODY])
    make_provider(settings, capture, capture_price).fetch()
    assert len(capture_price.calls) == 1
    assert "PRICIERHOT4" in capture_price.calls[0]


# --- 5: between two HOT offers, the cheaper one is chosen ------------------------


def test_two_hot_offers_prefers_the_cheaper(settings: Settings) -> None:
    offers = [
        category_offer("EXPENSIVEHOT5", price=1000, rating=4.5),
        category_offer("CHEAPHOT5", price=800, rating=4.8),
    ]
    capture = FakeCapture([listing_body(offers)])
    capture_price = FakeCapture([NOT_AVAILABLE_BODY])
    make_provider(settings, capture, capture_price).fetch()
    assert len(capture_price.calls) == 1
    assert "CHEAPHOT5" in capture_price.calls[0]


# --- 6: between two GOOD offers, the cheaper one is chosen -----------------------


def test_two_good_offers_prefers_the_cheaper(settings: Settings) -> None:
    offers = [
        category_offer("EXPENSIVEGOOD6", price=1300, rating=4.6),
        category_offer("CHEAPGOOD6", price=1200, rating=4.5),
    ]
    capture = FakeCapture([listing_body(offers)])
    capture_price = FakeCapture([NOT_AVAILABLE_BODY])
    make_provider(settings, capture, capture_price).fetch()
    assert len(capture_price.calls) == 1
    assert "CHEAPGOOD6" in capture_price.calls[0]


# --- 7: among several same-category (MATCH) candidates, the cheapest wins -------


def test_same_category_candidates_prefer_the_cheapest(settings: Settings) -> None:
    offers = [
        category_offer("MID7", price=1200, rating=4.1),
        category_offer("CHEAPEST7", price=1100, rating=4.1),
        category_offer("EXPENSIVE7", price=1250, rating=4.1),
    ]
    capture = FakeCapture([listing_body(offers)])
    capture_price = FakeCapture([NOT_AVAILABLE_BODY])
    make_provider(settings, capture, capture_price).fetch()
    assert len(capture_price.calls) == 1
    assert "CHEAPEST7" in capture_price.calls[0]


# --- 8: no potentially eligible candidate at all -> zero detail requests --------


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


# --- 9: an incomplete-price candidate still enters the shortlist ----------------


def test_incomplete_price_candidate_still_enters_shortlist(settings: Settings) -> None:
    # Every TUI listing offer starts with price_is_complete=False; this is
    # exactly what the detail request exists to try to confirm, so it must
    # never disqualify a candidate from the shortlist itself -- regardless of
    # HOT/GOOD/MATCH category, which is computed from listing data alone.
    offers = [raw_offer("ONLYCANDIDATE5")]
    capture = FakeCapture([listing_body(offers)])
    capture_price = FakeCapture([NOT_AVAILABLE_BODY])
    result = make_provider(settings, capture, capture_price).fetch()
    assert len(capture_price.calls) == 1
    assert "ONLYCANDIDATE5" in capture_price.calls[0]
    assert result[0].price_is_complete is False  # NOT_AVAILABLE stays unconfirmed


# --- 10: AVAILABLE + correct mandatory fees confirms price_is_complete=True ------


def test_available_confirmation_sets_price_is_complete_true(settings: Settings) -> None:
    offers = [raw_offer("CONFIRMME6", discountPerPersonPrice="1000", discountFullPrice="2000")]
    capture = FakeCapture([listing_body(offers)])
    capture_price = FakeCapture([charter_realtime_body("CONFIRMME6", total_price=2000)])
    result = make_provider(settings, capture, capture_price).fetch()
    assert result[0].price_is_complete is True
    assert result[0].price_per_person == 1030  # (2000 + 60 TFG/TFP) / 2


# --- 11: NOT_AVAILABLE stays unconfirmed, ineligible, no alert path --------------


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


# --- 12: max_detail_requests=1 is respected even with many eligible candidates ---


def test_max_detail_requests_one_is_respected_with_many_candidates(settings: Settings) -> None:
    offers = [raw_offer(f"MANY{i}", discountPerPersonPrice=str(1000 + i)) for i in range(5)]
    capture = FakeCapture([listing_body(offers)])
    capture_price = FakeCapture([NOT_AVAILABLE_BODY])
    make_provider(settings, capture, capture_price, max_detail_requests=1).fetch()
    assert len(capture_price.calls) == 1
    assert "MANY0" in capture_price.calls[0]  # cheapest of the five, same category
