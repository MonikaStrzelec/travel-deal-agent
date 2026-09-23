"""Injected HTTP/capture only: these tests never contact tui.pl or use Playwright."""

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from travel_deal_agent.config import Settings
from travel_deal_agent.config_types import ProviderConfig
from travel_deal_agent.providers.http import Response
from travel_deal_agent.providers.registry import build_providers
from travel_deal_agent.providers.tui import BASE, TuiProvider, robots_policy
from travel_deal_agent.providers.tui_errors import TuiBlocked, TuiStructureError, TuiTimeout
from travel_deal_agent.providers.tui_query import build_search_path

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
CONFIG: ProviderConfig = {"enabled": True, "interval_seconds": 3600}


def _raw_offer(**overrides: object) -> dict[str, object]:
    base = {
        "hotelCode": "MLA13036",
        "hotelName": "Bella Vista",
        "hotelStandard": 3.0,
        "offerCode": "KTWMLA20261204153020261204202612101740L06MLA130367DWVA02ROV7DWA02FCMM",
        "duration": 6,
        "offerUrl": "/wypoczynek/malta/bella-vista-mla13036/OfferCodeWS/KTWMLA20261204153020261204202612101740L06MLA130367DWVA02ROV7DWA02FCMM",
        "breadcrumbs": [{"label": "Malta"}],
        "discountFullPrice": "2194",
        "originalFullPrice": "2194",
        "discountPerPersonPrice": "1097",
        "originalPerPersonPrice": "1097",
        "departureDate": "04.12.2026",
        "returnDate": "10.12.2026",
        "departureTime": "15:30",
        "departureAirport": "Katowice",
        "boardType": "Trzy posiłki",
        "boardCode": "GT06-FB",
        "tripAdvisorRating": 2.9,
        "tripAdvisorReviewsNo": 2374,
        "participants": "2 Dorosłych + 0 Dzieci",
        "currency": "PLN",
        "soldOut": False,
    }
    base.update(overrides)
    return base


ONE_OFFER_BODY = json.dumps({"offers": [_raw_offer()]})
TWO_OFFER_BODY = json.dumps(
    {
        "offers": [
            _raw_offer(),
            _raw_offer(
                hotelCode="VAR11019",
                hotelName="Hotel Koral",
                offerCode="WAWBOJ20270907134520270907202709141750L07VAR11019DZX1HA02ROHDZX1A02FCYY",
                duration=7,
                offerUrl="/wypoczynek/bulgaria/hotel-koral-var11019/OfferCodeWS/WAWBOJ20270907134520270907202709141750L07VAR11019DZX1HA02ROHDZX1A02FCYY",
                breadcrumbs=[{"label": "Bułgaria"}],
                discountFullPrice="2800",
                originalFullPrice="2800",
                discountPerPersonPrice="1400",
                originalPerPersonPrice="1400",
                departureDate="07.09.2027",
                returnDate="14.09.2027",
                departureAirport="Warszawa-Chopina",
                boardType="Dwa posiłki",
                boardCode="GT06-HB",
            ),
        ]
    }
)
# offerStatus alone (no offerCode/priceDetails) is exactly what a real NOT_AVAILABLE
# response looks like (confirmed live, 2026-09-22) -- the safe, non-crashing default
# for tests that don't care about detail confirmation itself.
NOT_AVAILABLE_BODY = json.dumps({"offerStatus": "NOT_AVAILABLE", "alternativeOffers": []})


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


class FakeCapture:
    def __init__(self, body: str | Exception = ONE_OFFER_BODY) -> None:
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
        capture_price if capture_price is not None else FakeCapture(body=NOT_AVAILABLE_BODY),
        sleep=lambda _: None,
        wall_clock=lambda: NOW,
    )


def test_fetch_reads_robots_then_captures_and_returns_offers(settings: Settings) -> None:
    # Arrange
    transport = FakeTransport([response("User-agent: *\nAllow: /\nDisallow: /api/")])
    capture = FakeCapture()
    # Act
    offers = provider(settings, transport, capture).fetch()
    # Assert
    assert len(offers) == 1
    assert offers[0].provider == "tui"
    assert offers[0].found_at == NOW and offers[0].last_seen == NOW
    assert transport.urls == [BASE + "/robots.txt"]
    expected_path = build_search_path(settings.filters)
    assert capture.calls == [(BASE + expected_path, 20)]


def test_disallowed_search_path_is_never_captured(settings: Settings) -> None:
    # Arrange: robots.txt disallows the whole search-results path this provider uses.
    transport = FakeTransport(
        [response("User-agent: *\nDisallow: /wypoczynek/wyniki-wyszukiwania-samolot")]
    )
    capture = FakeCapture()
    # Act / Assert
    with pytest.raises(ValueError, match="robots"):
        provider(settings, transport, capture).fetch()
    assert transport.urls == [BASE + "/robots.txt"]
    assert capture.calls == []


@pytest.mark.parametrize(
    "robots",
    ["<html>CAPTCHA</html>", "User-agent: Other\nDisallow: /"],
)
def test_robots_fail_closed(robots: str, settings: Settings) -> None:
    # Arrange
    transport = FakeTransport([response(robots)])
    capture = FakeCapture()
    # Act / Assert
    with pytest.raises(ValueError):
        provider(settings, transport, capture).fetch()
    assert len(transport.urls) == 1
    assert capture.calls == []


@pytest.mark.parametrize("status", [301, 403, 429, 503])
def test_no_retry_or_redirect_on_robots_http_failure(status: int, settings: Settings) -> None:
    # Arrange
    transport = FakeTransport([response("", status)])
    capture = FakeCapture()
    # Act / Assert
    with pytest.raises(ValueError, match="HTTP"):
        provider(settings, transport, capture).fetch()
    assert len(transport.urls) == 1
    assert capture.calls == []


def test_api_path_is_never_requested_directly(settings: Settings) -> None:
    # A defensive regression: the real backend lives under a robots-disallowed /api/
    # path. This provider only ever fetches robots.txt directly and opens the
    # confirmed-allowed /wypoczynek/... URL through the (here, fake) browser capture.
    transport = FakeTransport([response("User-agent: *\nAllow: /\nDisallow: /api/")])
    capture = FakeCapture()
    # Act
    provider(settings, transport, capture).fetch()
    # Assert
    assert all("/api/" not in url for url in transport.urls)
    assert all("/api/" not in url for url, _ in capture.calls)
    assert all(url.startswith(BASE + "/wypoczynek/") for url, _ in capture.calls)


def test_crawl_delay_is_honored_before_capture(settings: Settings) -> None:
    # Arrange
    transport = FakeTransport([response("User-agent: *\nDisallow: /api/\nCrawl-delay: 3")])
    capture = FakeCapture()
    capture_price = FakeCapture(body=NOT_AVAILABLE_BODY)
    waits: list[float] = []
    p = TuiProvider(
        CONFIG,
        settings.filters,
        transport,
        capture,
        capture_price,
        sleep=waits.append,
        wall_clock=lambda: NOW,
    )
    # Act
    p.fetch()
    # Assert: once before the search-results navigation, once before the one
    # detail-confirmation navigation -- the site-wide crawl-delay applies to both.
    assert waits == [3, 3]


def test_capture_failure_propagates(settings: Settings) -> None:
    # Arrange
    transport = FakeTransport([response("User-agent: *\nDisallow: /api/")])
    capture = FakeCapture(body=TuiBlocked("TUI requires human verification"))
    # Act / Assert
    with pytest.raises(TuiBlocked):
        provider(settings, transport, capture).fetch()


@pytest.mark.parametrize("error", [TuiTimeout("timed out"), TuiStructureError("ambiguous")])
def test_other_capture_failures_propagate_distinctly(error: Exception, settings: Settings) -> None:
    # Arrange
    transport = FakeTransport([response("User-agent: *\nDisallow: /api/")])
    capture = FakeCapture(body=error)
    # Act / Assert
    with pytest.raises(type(error)):
        provider(settings, transport, capture).fetch()


def test_schema_change_in_the_captured_body_fails_closed(settings: Settings) -> None:
    # Arrange
    transport = FakeTransport([response("User-agent: *\nDisallow: /api/")])
    capture = FakeCapture(body="<html>not json</html>")
    # Act / Assert
    with pytest.raises(ValueError):
        provider(settings, transport, capture).fetch()


# --- bounded real-time detail confirmation (max 1 candidate per cycle by default) --


def test_detail_confirmation_defaults_to_one_candidate(settings: Settings) -> None:
    # Arrange: two listed offers, both with a usable URL.
    transport = FakeTransport([response("User-agent: *\nDisallow: /api/")])
    capture = FakeCapture(body=TWO_OFFER_BODY)
    capture_price = FakeCapture(body=NOT_AVAILABLE_BODY)
    # Act
    offers = provider(settings, transport, capture, capture_price).fetch()
    # Assert: only the first (cheapest, listing-order) offer gets a detail request.
    assert len(offers) == 2
    assert len(capture_price.calls) == 1
    assert capture_price.calls[0][0] == offers[0].url


def test_max_detail_requests_zero_confirms_nothing(settings: Settings) -> None:
    # Arrange
    transport = FakeTransport([response("User-agent: *\nDisallow: /api/")])
    capture = FakeCapture(body=TWO_OFFER_BODY)
    capture_price = FakeCapture(body=NOT_AVAILABLE_BODY)
    # Act
    offers = provider(settings, transport, capture, capture_price, max_detail_requests=0).fetch()
    # Assert
    assert len(offers) == 2
    assert capture_price.calls == []


def test_detail_confirmation_failure_does_not_crash_the_cycle(settings: Settings) -> None:
    # Arrange: the detail browser capture itself fails (timeout/blocked/ambiguous).
    transport = FakeTransport([response("User-agent: *\nDisallow: /api/")])
    capture = FakeCapture()
    capture_price = FakeCapture(body=TuiTimeout("no matching response"))
    # Act
    offers = provider(settings, transport, capture, capture_price).fetch()
    # Assert: the cycle completes; the listed offer is kept, unconfirmed.
    assert len(offers) == 1
    assert offers[0].price_is_complete is False
    assert "no matching response" in (offers[0].price_verification_reason or "")


def test_detail_confirmation_malformed_response_does_not_crash_the_cycle(
    settings: Settings,
) -> None:
    # Arrange
    transport = FakeTransport([response("User-agent: *\nDisallow: /api/")])
    capture = FakeCapture()
    capture_price = FakeCapture(body="<html>not json</html>")
    # Act
    offers = provider(settings, transport, capture, capture_price).fetch()
    # Assert
    assert len(offers) == 1
    assert offers[0].price_is_complete is False
    assert offers[0].price_verification_reason is not None


def _charter_available_body(**price_overrides: object) -> str:
    """A fully valid, reconciled AVAILABLE real-time response for the default
    ONE_OFFER_BODY listing offer (MLA13036), confirmed as a charter-flight
    package tour -- the exact shape (tags/analyticsData/offerTravelType) that
    unlocks the officially confirmed TFG+TFP rate in `tui_price.py`."""
    price_details = {
        "totalPrice": 2194,
        "totalDiscountPrice": 2194,
        "priceGuaranteeFund": 60,
        "priceGuaranteeFundInfo": "Turystyczny Fundusz Gwarancyjny",
        "priceDifference": 0,
        "currency": "PLN",
        "pricePerPerson": 1097,
        "factor": 1.0,
        "discountPercentage": 0,
    }
    price_details.update(price_overrides)
    return json.dumps(
        {
            "offerStatus": "AVAILABLE",
            "offerCode": "KTWMLA20261204153020261204202612101740L06MLA130367DWVA02ROV7DWA02FCMM",
            "priceDetails": price_details,
            "travellerCount": {"adults": 2, "children": 0},
            "outboundFlight": {"departureAirportCode": "KTW"},
            "accommodations": [{"hotelCode": "MLA13036", "duration": 6}],
            "startDate": "2026-12-04T15:30:00",
            "endDate": "2026-12-10T17:40:00",
            "tags": ["CHARTER_FLIGHT", "FIRST_MINUTE"],
            "offerTravelType": "BYPLANE",
            "analyticsData": {"values": {"flight_type": "CHART"}},
        }
    )


def test_detail_confirmation_confirms_price_for_a_charter_package_offer(
    settings: Settings,
) -> None:
    # Arrange: end-to-end through the real TuiProvider flow (Sun City-shaped
    # rate: 2194 total + confirmed TFG+TFP 60 for 2 adults = 2254 total / 1127
    # per person -- same confirmed formula as the real Sun City example (2786 +
    # 60 = 2846 / 1423), just against this file's default listing fixture).
    transport = FakeTransport([response("User-agent: *\nDisallow: /api/")])
    capture = FakeCapture()
    capture_price = FakeCapture(body=_charter_available_body())
    # Act
    offers = provider(settings, transport, capture, capture_price).fetch()
    # Assert
    assert len(offers) == 1
    assert offers[0].price_is_complete is True
    assert offers[0].booking_total_price == Decimal("2254")
    assert offers[0].total_price == Decimal("2254")
    assert offers[0].price_per_person == Decimal("1127")
    assert offers[0].variant_verified is True
    assert offers[0].sale_status == "available"


def test_detail_confirmation_stays_unconfirmed_for_a_non_charter_offer(
    settings: Settings,
) -> None:
    # Arrange: same reconciled AVAILABLE response, but not structurally
    # confirmed as a charter package -- the only confirmed TFG/TFP rate does
    # not apply, so this must stay unconfirmed rather than guess a rate.
    body = json.loads(_charter_available_body())
    body["tags"] = ["FIRST_MINUTE"]
    body["analyticsData"]["values"]["flight_type"] = "LINE"
    transport = FakeTransport([response("User-agent: *\nDisallow: /api/")])
    capture = FakeCapture()
    capture_price = FakeCapture(body=json.dumps(body))
    # Act
    offers = provider(settings, transport, capture, capture_price).fetch()
    # Assert
    assert len(offers) == 1
    assert offers[0].price_is_complete is False
    assert "CHARTER_PACKAGE_NOT_CONFIRMED" in (offers[0].price_verification_reason or "")
    # Never accidentally routed around the intentional gate either.
    assert "tui" not in settings.filters.get("accept_incomplete_price_from", [])


def test_invalid_filters_are_rejected_at_construction(settings: Settings) -> None:
    # Arrange: an unsupported business configuration (three adults).
    bad_filters = dict(settings.filters)
    bad_filters["people"] = 3
    # Act / Assert: fails fast, before any request or browser capture.
    with pytest.raises(ValueError, match="two adults"):
        TuiProvider(CONFIG, bad_filters, FakeTransport([]), FakeCapture())  # type: ignore[arg-type]


def test_registry_builds_a_disabled_tui_provider_without_network_access(
    settings: Settings,
) -> None:
    # Arrange: an explicitly disabled local override -- production config.json
    # now has TUI enabled (2026-09-22 MVP go-live), so this test no longer
    # relies on that default; it only checks the registry's disabled-provider
    # behavior, matching the ITAKA/Rainbow pattern.
    disabled: ProviderConfig = {**settings.providers["tui"], "enabled": False}
    config: dict[str, ProviderConfig] = {"tui": disabled}
    # Act
    sources = build_providers(config, filters=settings.filters)
    # Assert
    assert sources == []


def test_registry_builds_an_enabled_tui_provider(settings: Settings) -> None:
    # Arrange
    enabled: ProviderConfig = {**settings.providers["tui"], "enabled": True}
    # Act: construction validates filters but performs no network/browser access.
    sources = build_providers({"tui": enabled}, filters=settings.filters)
    # Assert
    assert [s.name for s in sources] == ["tui"]
    assert isinstance(sources[0], TuiProvider)


def test_robots_policy_is_a_conservative_union() -> None:
    # Arrange / Act / Assert
    assert robots_policy("User-agent: *\nDisallow: /api/\nCrawl-delay: 5", "/wypoczynek/x") == 5
    with pytest.raises(ValueError, match="forbidden"):
        robots_policy("User-agent: *\nDisallow: /wypoczynek/", "/wypoczynek/x")
    with pytest.raises(ValueError, match="robots.txt"):
        robots_policy("<html>not robots</html>", "/wypoczynek/x")
