"""Full offline TUI flow: listing -> normalize -> real-time confirmation ->
complete price -> filtering.matches() -> ranking -> storage/notification
eligibility. Every response here is injected; no network, no Playwright.
"""

import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from travel_deal_agent.config import Settings
from travel_deal_agent.config_types import ProviderConfig
from travel_deal_agent.models import Offer, duplicate_key
from travel_deal_agent.notification_content import NotificationMessage
from travel_deal_agent.notifications import LogNotifier, Notifier
from travel_deal_agent.providers.base import Provider
from travel_deal_agent.providers.http import Response
from travel_deal_agent.providers.tui import TuiProvider
from travel_deal_agent.providers.tui_data import normalize_offer
from travel_deal_agent.ranking import deduplicate
from travel_deal_agent.scheduler import Scheduler
from travel_deal_agent.storage import Store

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
CONFIG: ProviderConfig = {"enabled": True, "interval_seconds": 3600, "max_detail_requests": 1}

# Chosen so the confirmed complete price lands inside every business rule at
# once: HB board, 4-star hotel in Turkey (country override also requires 4),
# KTW airport, 7 nights, and -- crucially -- (2440 total + 60 confirmed TFG+TFP
# for 2 adults) / 2 = 1250 PLN/person, comfortably under the 1500 cap and in
# the 1000-1500 HB band.
RAW_OFFER: dict[str, object] = {
    "hotelCode": "AYT99001",
    "hotelName": "E2E Test Resort",
    "hotelStandard": 4.0,
    "offerCode": "KTWAYT20261212020020261212202612190740L07AYT99001TESTHA02ROHTESTA02FCYY",
    "duration": 7,
    "offerUrl": (
        "/wypoczynek/turcja/e2e-test-resort-ayt99001/OfferCodeWS/"
        "KTWAYT20261212020020261212202612190740L07AYT99001TESTHA02ROHTESTA02FCYY"
    ),
    "breadcrumbs": [{"label": "Turcja"}],
    "discountFullPrice": "2440",
    "originalFullPrice": "2440",
    "discountPerPersonPrice": "1220",
    "originalPerPersonPrice": "1220",
    "departureDate": "12.12.2026",
    "returnDate": "19.12.2026",
    "departureTime": "02:00",
    "departureAirport": "Katowice",
    "boardType": "Dwa posiłki",
    "boardCode": "GT06-HB",
    "tripAdvisorRating": 4.0,
    "tripAdvisorReviewsNo": 500,
    "participants": "2 Dorosłych + 0 Dzieci",
    "currency": "PLN",
    "soldOut": False,
}
LISTING_BODY = json.dumps({"offers": [RAW_OFFER]})


def charter_realtime_body(**overrides: object) -> str:
    body: dict[str, object] = {
        "offerStatus": "AVAILABLE",
        "offerCode": RAW_OFFER["offerCode"],
        "priceDetails": {
            "totalPrice": 2440,
            "totalDiscountPrice": 2440,
            "priceGuaranteeFund": 60,
            "priceGuaranteeFundInfo": "Turystyczny Fundusz Gwarancyjny",
            "priceDifference": 0,
            "currency": "PLN",
            "pricePerPerson": 1220,
            "factor": 1.0,
            "discountPercentage": 0,
        },
        "travellerCount": {"adults": 2, "children": 0},
        "outboundFlight": {"departureAirportCode": "KTW"},
        "accommodations": [{"hotelCode": "AYT99001", "duration": 7}],
        "startDate": "2026-12-12T02:00:00",
        "endDate": "2026-12-19T07:40:00",
        "tags": ["CHARTER_FLIGHT"],
        "offerTravelType": "BYPLANE",
        "analyticsData": {"values": {"flight_type": "CHART"}},
    }
    body.update(overrides)
    return json.dumps(body)


NOT_AVAILABLE_BODY = json.dumps({"offerStatus": "NOT_AVAILABLE", "alternativeOffers": []})


class FakeTransport:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = responses

    def get(self, url: str, timeout: float) -> Response:
        return self.responses.pop(0)


def robots_response() -> Response:
    return Response(200, "User-agent: *\nDisallow: /api/", {})


class FakeCapture:
    def __init__(self, body: str) -> None:
        self.body = body

    def __call__(self, url: str, timeout_seconds: float) -> str:
        return self.body


def tui_provider(settings: Settings, capture_price_body: str) -> TuiProvider:
    return TuiProvider(
        CONFIG,
        settings.filters,
        FakeTransport([robots_response()]),
        FakeCapture(LISTING_BODY),
        FakeCapture(capture_price_body),
        sleep=lambda _: None,
        wall_clock=lambda: NOW,
    )


class FixtureTuiSource(Provider):
    """Wraps a real TuiProvider so the scheduler's registry-free injection works
    (mirrors the FixtureProvider pattern already used in test_ratings.py)."""

    name = "tui"

    def __init__(self, provider: TuiProvider) -> None:
        self._provider = provider

    def fetch(self) -> list[Offer]:
        return self._provider.fetch()


def run_tui_only(settings: Settings, store: Store, provider: TuiProvider) -> list[Offer]:
    enabled_providers: dict[str, ProviderConfig] = {
        name: {**cfg, "enabled": name == "tui"} for name, cfg in settings.providers.items()
    }
    scoped_settings = replace(settings, providers=enabled_providers)
    scheduler = Scheduler(
        scoped_settings,
        [FixtureTuiSource(provider)],
        store,
        LogNotifier(),
        today=lambda: NOW.date(),
    )
    return scheduler.run_once(force=True)


# --- happy path: reaches eligible/notification path end to end ------------------


def test_confirmed_charter_offer_reaches_storage_and_notification_eligibility(
    settings: Settings, store: Store
) -> None:
    # Arrange
    provider = tui_provider(settings, charter_realtime_body())
    # Act
    result = run_tui_only(settings, store, provider)
    # Assert: survived filtering.matches(), ranking and persistence.
    assert len(result) == 1
    offer = result[0]
    assert offer.provider == "tui"
    assert offer.price_is_complete is True
    assert offer.price_per_person == Decimal("1250")
    assert offer.total_price == Decimal("2500")
    assert offer.final_score is not None
    stored = store.get_offer("tui", offer.offer_id)
    assert stored is not None
    assert stored.price_is_complete is True


# --- fail-closed paths: must never reach eligible/notification --------------------


def test_not_available_offer_never_reaches_eligibility(settings: Settings, store: Store) -> None:
    provider = tui_provider(settings, NOT_AVAILABLE_BODY)
    result = run_tui_only(settings, store, provider)
    assert result == []
    stored = store.get_offer("tui", str(RAW_OFFER["offerCode"]))
    assert stored is not None
    assert stored.price_is_complete is False


def test_non_charter_confirmed_offer_never_reaches_eligibility(
    settings: Settings, store: Store
) -> None:
    # Arrange: AVAILABLE, price reconciled, but not a confirmed charter package
    # -- the only confirmed TFG/TFP rate does not apply.
    body = json.loads(charter_realtime_body())
    body["tags"] = ["FIRST_MINUTE"]
    body["analyticsData"]["values"]["flight_type"] = "LINE"
    provider = tui_provider(settings, json.dumps(body))
    # Act
    result = run_tui_only(settings, store, provider)
    # Assert
    assert result == []
    stored = store.get_offer("tui", str(RAW_OFFER["offerCode"]))
    assert stored is not None
    assert stored.price_is_complete is False


def test_realtime_price_change_propagates_to_eligibility_and_notification(
    settings: Settings, store: Store
) -> None:
    """Listing quoted 2440 total (1220/person); realtime reports 2450
    (priceDifference=10) -- a nonzero priceDifference is informational only
    (see tui_price.py), so this must still confirm, and every downstream
    consumer (filtering.matches(), Store persistence, the rendered Telegram
    preview) must use the realtime price, never the stale listing quote."""
    body = json.loads(charter_realtime_body())
    body["priceDetails"]["totalPrice"] = 2450
    body["priceDetails"]["totalDiscountPrice"] = 2450
    body["priceDetails"]["pricePerPerson"] = 1225
    body["priceDetails"]["priceDifference"] = 10
    provider = tui_provider(settings, json.dumps(body))
    notifier = SpyNotifier()
    scheduler = Scheduler(
        replace(
            settings,
            providers={
                name: {**cfg, "enabled": name == "tui"} for name, cfg in settings.providers.items()
            },
        ),
        [FixtureTuiSource(provider)],
        store,
        notifier,
        today=lambda: NOW.date(),
    )

    result = scheduler.run_once(force=True)

    assert len(result) == 1
    offer = result[0]
    assert offer.price_is_complete is True
    assert offer.total_price == Decimal("2510")  # 2450 + 60 confirmed TFG+TFP
    assert offer.price_per_person == Decimal("1255")  # never the listing's 1220

    stored = store.get_offer("tui", offer.offer_id)
    assert stored is not None
    assert stored.price_per_person == Decimal("1255")

    assert len(notifier.sent) == 1
    kind, text = notifier.sent[0]
    assert kind == "new_offer"
    assert "1255 zł/os." in text  # Telegram preview uses the confirmed realtime price
    assert "1220 zł/os." not in text  # never the stale listing price


def test_unknown_fee_field_never_reaches_eligibility(settings: Settings, store: Store) -> None:
    # Arrange: a possible new mandatory fee TUI has never disclosed before.
    body = json.loads(charter_realtime_body())
    body["priceDetails"]["someNewMandatoryFee"] = 25
    provider = tui_provider(settings, json.dumps(body))
    # Act
    result = run_tui_only(settings, store, provider)
    # Assert
    assert result == []
    stored = store.get_offer("tui", str(RAW_OFFER["offerCode"]))
    assert stored is not None
    assert stored.price_is_complete is False


# --- pagination-adjacent dedup: distinct dates never collapse into one offer -----


def test_different_departure_dates_are_never_deduplicated_into_one_offer() -> None:
    # Documents the safety net that matters once pagination is eventually
    # implemented (PAGINATION_LIVE_CONFIRMATION_NEEDED): two offers for the same
    # hotel but different departure dates -- as would come from two different
    # result pages -- must never collapse into a single entry.
    same_hotel_later_date = dict(RAW_OFFER)
    same_hotel_later_date["offerCode"] = str(RAW_OFFER["offerCode"]) + "LATER"
    same_hotel_later_date["departureDate"] = "19.12.2026"
    same_hotel_later_date["returnDate"] = "26.12.2026"

    first = normalize_offer(RAW_OFFER, NOW, source="search_xhr")
    second = normalize_offer(same_hotel_later_date, NOW, source="search_xhr")

    assert duplicate_key(first) != duplicate_key(second)
    assert len(deduplicate([first, second])) == 2


# --- full offline pipeline: price history, alert events, climate, outbox --------


class SpyNotifier(Notifier):
    """Captures rendered messages instead of delivering anywhere real."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, notification: object) -> None:
        message = NotificationMessage.from_notification(notification)  # type: ignore[arg-type]
        self.sent.append((message.kind, message.render()))


def _charter_body_for_total_price(total: int) -> str:
    body = json.loads(charter_realtime_body())
    body["priceDetails"]["totalPrice"] = total
    body["priceDetails"]["totalDiscountPrice"] = total
    body["priceDetails"]["pricePerPerson"] = total // 2
    return json.dumps(body)


def test_tui_price_history_drives_new_new_low_and_price_drop_alerts(
    settings: Settings, store: Store
) -> None:
    """One TUI offer observed across four scans: NEW, then a drop that is also
    the historical minimum (NEW_LOW), an increase that alerts nothing, and
    finally a drop that is *not* a new historical minimum (PRICE_DROP).

    Exercises the full production chain for TUI specifically: listing ->
    realtime confirmation -> filtering -> ranking -> Store.observe's price
    history/alert classification -> outbox -> compact Telegram-style
    rendering (including the climate line) -- all offline, no network.
    """
    destination_raw_offer = dict(RAW_OFFER)
    destination_raw_offer["breadcrumbs"] = [{"label": "Turcja"}, {"label": "Side"}]
    listing_body = json.dumps({"offers": [destination_raw_offer]})

    def provider_for(total_price: int) -> TuiProvider:
        return TuiProvider(
            CONFIG,
            settings.filters,
            FakeTransport([robots_response()]),
            FakeCapture(listing_body),
            FakeCapture(_charter_body_for_total_price(total_price)),
            sleep=lambda _: None,
            wall_clock=lambda: NOW,
        )

    notifier = SpyNotifier()

    def run_cycle(total_price: int) -> None:
        scheduler = Scheduler(
            replace(
                settings,
                providers={
                    name: {**cfg, "enabled": name == "tui"}
                    for name, cfg in settings.providers.items()
                },
            ),
            [FixtureTuiSource(provider_for(total_price))],
            store,
            notifier,
            today=lambda: NOW.date(),
        )
        scheduler.run_once(force=True)

    # Cycle 1: first-ever observation -> NEW, price_per_person = 1250.
    run_cycle(2440)
    # Cycle 2: drop to 1130, also the historical minimum so far -> NEW_LOW.
    run_cycle(2200)
    # Cycle 3: price rises to 1330 -> no alert, only price history recorded.
    run_cycle(2600)
    # Cycle 4: drop to 1280, below cycle 3 but still above the 1130 minimum -> PRICE_DROP.
    run_cycle(2500)

    kinds = [kind for kind, _ in notifier.sent]
    assert kinds == ["new_offer", "new_low", "price_drop"]

    prices = store.price_history("tui", str(RAW_OFFER["offerCode"]))
    assert prices == [Decimal("1250"), Decimal("1130"), Decimal("1330"), Decimal("1280")]

    stats = store.price_stats("tui", str(RAW_OFFER["offerCode"]))
    assert stats is not None
    assert stats.lowest_price == Decimal("1130")

    new_offer_text = dict(notifier.sent)["new_offer"]
    new_low_text = dict(notifier.sent)["new_low"]
    price_drop_text = dict(notifier.sent)["price_drop"]

    # Compact Polish rendering, provider-agnostic event labels.
    assert "NAJNIŻSZA CENA" in new_low_text
    assert "SPADEK CENY" in price_drop_text
    # Climate V0 line: "Side" resolves to the Antalya riviera region.
    assert "Typowo w grudniu" in new_offer_text
    assert "°C" in new_offer_text
