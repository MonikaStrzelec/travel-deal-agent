"""Complete local alert content and transport-independent snapshot behavior."""

import logging
from dataclasses import replace
from decimal import Decimal

import pytest

from travel_deal_agent.config import Settings
from travel_deal_agent.models import Offer
from travel_deal_agent.notification_content import NotificationMessage
from travel_deal_agent.notifications import ConsoleNotifier, deliver_pending
from travel_deal_agent.pipeline import OfferPipeline
from travel_deal_agent.ranking import score
from travel_deal_agent.storage import Store


def test_full_message_contains_offer_details(offer: Offer, store: Store) -> None:
    candidate = replace(
        offer,
        provider="itaka",
        rating=5.3,
        provider_rating_max=6,
        google_rating={"rating": 4.4},
        google_rating_max=5,
        external_rating_status="verified",
        final_score=5.25,
    )
    store.observe(candidate, True, Decimal("100"))

    message = NotificationMessage.from_notification(store.pending()[0]).render()

    for expected in (
        "Hotel: Sunny Demo",
        "Country / region: GR / Crete",
        "Travel agency: ITAKA",
        "Price per person: 1299.00 PLN",
        "Total for 2 travelers: 2598.00 PLN",
        "Duration: 7 days",
        "Departure airport: LCJ",
        "Hotel stars: 3",
        "Agency rating: 5.3/6",
        "Google rating: 4.4/5",
        "Board: all_inclusive",
        "Offer URL: https://example.invalid/offers/0",
        "Final score: 5.250",
    ):
        assert expected in message
    assert "Price drop" not in message


def test_unverified_google_data_is_not_presented_as_verified(offer: Offer, store: Store) -> None:
    candidate = replace(offer, google_rating={"rating": 4.9}, google_rating_max=5)
    store.observe(candidate, True, Decimal("100"))

    message = NotificationMessage.from_notification(store.pending()[0]).render()

    assert "Google rating: not available" in message
    assert "4.9/5" not in message


def test_missing_data_and_calculated_party_total(offer: Offer, store: Store) -> None:
    store.observe(replace(offer, total_price=None, board_type=None, url=None), True, Decimal("100"))

    message = NotificationMessage.from_notification(store.pending()[0]).render()

    assert "Total for 2 travelers: 2598.00 PLN (calculated)" in message
    assert "Board: not available" in message
    assert "Offer URL: not available" in message
    assert "Final score: not available" in message
    assert "scale unknown" in message


def test_price_drop_message_uses_previous_alert_baseline(offer: Offer, store: Store) -> None:
    store.observe(offer, True, Decimal("100"))
    store.observe(replace(offer, price_per_person=Decimal("1199")), True, Decimal("100"))

    message = NotificationMessage.from_notification(store.pending()[-1]).render()

    assert "price_drop" in message
    assert "Price drop per person: 100.00 PLN (previous alert: 1299.00 PLN)" in message


def test_pipeline_persists_final_score_for_stable_retries(
    offer: Offer, store: Store, settings: Settings
) -> None:
    pipeline = OfferPipeline(settings, store)
    result = pipeline.finalize([offer])[0]
    notification = store.pending()[0]
    original = NotificationMessage.from_notification(notification)

    store.observe(replace(result, final_score=999), True, Decimal("100"))
    retried = NotificationMessage.from_notification(store.pending()[0])

    assert result.final_score == pytest.approx(score(offer, settings.ranking, "1500"))
    assert original.offer.provider_rating_max == 10
    assert retried.render() == original.render()
    assert retried.offer.final_score == result.final_score


def test_incomplete_price_gets_a_listing_disclaimer(offer: Offer, store: Store) -> None:
    # Arrange: a listing-only price (e.g. Wakacje.pl) reaching the outbox at all
    # already means filtering.matches() accepted it via the provider whitelist;
    # rendering must disclose that the price is unconfirmed, not hide it.
    candidate = replace(offer, provider="wakacje.pl", price_is_complete=False)
    store.observe(candidate, True, Decimal("100"))

    message = NotificationMessage.from_notification(store.pending()[0]).render()

    assert "Price is from the listing — not yet confirmed at checkout/booking." in message


def test_complete_price_has_no_listing_disclaimer(offer: Offer, store: Store) -> None:
    # Arrange: the default fixture offer already has price_is_complete=True.
    store.observe(offer, True, Decimal("100"))

    message = NotificationMessage.from_notification(store.pending()[0]).render()

    assert "Price is from the listing" not in message


def test_console_notifier_logs_full_message_once(
    offer: Offer, store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    store.observe(offer, True, Decimal("100"))

    with caplog.at_level(logging.INFO, logger="travel_deal_agent.notifications"):
        deliver_pending(store, ConsoleNotifier())
        deliver_pending(store, ConsoleNotifier())

    assert caplog.text.count("Travel Deal Agent | new_offer") == 1
    assert "Total for 2 travelers: 2598.00 PLN" in caplog.text
    assert store.pending() == []
