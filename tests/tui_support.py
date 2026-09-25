"""Shared offline TUI test fakes and payloads; never contacts tui.pl."""

import json

from travel_deal_agent.providers.http import Response

# offerStatus alone (no offerCode/priceDetails) is exactly what a real NOT_AVAILABLE
# response looks like (confirmed live, 2026-09-22) -- the safe, non-crashing default
# for tests that don't care about detail confirmation itself.
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


class FakeTransport:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, timeout: float) -> Response:
        self.urls.append(url)
        return self.responses.pop(0)


def robots_response() -> Response:
    return Response(200, "User-agent: *\nDisallow: /api/", {})
