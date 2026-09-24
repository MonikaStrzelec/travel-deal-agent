"""Compact, Polish-language alert content and transport-independent snapshot behavior."""

import logging
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from travel_deal_agent.config import Settings
from travel_deal_agent.models import LocalMandatoryCost, Offer, OperatorFee
from travel_deal_agent.notification_content import NotificationMessage
from travel_deal_agent.notifications import ConsoleNotifier, deliver_pending
from travel_deal_agent.pipeline import OfferPipeline
from travel_deal_agent.ranking import score
from travel_deal_agent.storage import Store


def test_full_message_contains_offer_details(
    offer: Offer, store: Store, settings: Settings
) -> None:
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
    store.observe(candidate, True)

    message = NotificationMessage.from_notification(store.pending()[0]).render(
        settings.attractiveness, settings.filters["provider_ratings"]
    )

    for expected in (
        # LCJ (strong airport) + itaka rating 5.3/6 = 86% (strong hotel quality)
        # -- two strong areas, no weak one -- is exactly the HOT combination.
        "🔥 NOWA • Szczególnie ciekawa",
        "🏨 Sunny Demo ★★★ • Grecja • Crete",
        "⭐ 5,3/6 (Google: 4,4/5)",
        "🍽 all_inclusive",
        "💰 1299 zł/os. (2598 zł / 2 osoby) • 217 zł/os./noc",
        "🛫 Łódź • 7 dni / 6 nocy",
        '<a href="https://example.invalid/offers/0">Zobacz ofertę</a>',
    ):
        assert expected in message
    assert "final_score" not in message.lower()
    assert "5,25" not in message and "5.25" not in message
    assert "Poprzednio" not in message
    assert "niepotwierdzona" not in message


def test_zo_board_renders_as_wedlug_programu(offer: Offer, store: Store) -> None:
    # Arrange: ZO ("Wedlug programu") is a normal canonical board (boards.py);
    # notification_content.BOARD_LABELS_PL renders it with its full Polish label.
    candidate = replace(offer, provider="wakacje.pl", board_type="ZO")
    store.observe(candidate, True)

    message = NotificationMessage.from_notification(store.pending()[0]).render()

    assert "🍽 Według programu (ZO)" in message


def test_unverified_google_data_is_not_shown(offer: Offer, store: Store) -> None:
    candidate = replace(offer, google_rating={"rating": 4.9}, google_rating_max=5)
    store.observe(candidate, True)

    message = NotificationMessage.from_notification(store.pending()[0]).render()

    assert "Google" not in message
    assert "4,9" not in message


def test_missing_data_is_omitted_not_placeholdered(offer: Offer, store: Store) -> None:
    candidate = replace(
        offer,
        total_price=None,
        board_type=None,
        url=None,
        rating=None,
        hotel_stars=None,
        country=None,
        destination=None,
    )
    store.observe(candidate, True)

    message = NotificationMessage.from_notification(store.pending()[0]).render()

    # Total is recalculated from price_per_person * number_of_people.
    assert "💰 1299 zł/os. (2598 zł / 2 osoby)" in message
    assert "🍽" not in message
    assert "🔗" not in message
    assert "⭐" not in message
    assert "★" not in message
    assert "not available" not in message
    assert "None" not in message


def test_price_drop_message_shows_previous_price(offer: Offer, store: Store) -> None:
    # A drop that is not also a new historical low (the price had already
    # gone lower before, then climbed back up) renders as SPADEK CENY.
    store.observe(offer, True)  # 1299
    store.observe(replace(offer, price_per_person=Decimal("1000")), True)  # new low
    store.observe(replace(offer, price_per_person=Decimal("1200")), True)  # back up; no alert

    events = store.observe(replace(offer, price_per_person=Decimal("1100")), True)
    assert events == ["price_changed", "price_drop"]
    message = NotificationMessage.from_notification(store.pending()[-1]).render()

    assert "SPADEK CENY" in message
    assert "💰 1100 zł/os." in message
    assert "📉 Było 1200 zł/os. • spadek 100 zł" in message


def test_new_low_message_shows_previous_price(offer: Offer, store: Store) -> None:
    store.observe(offer, True)  # 1299

    events = store.observe(replace(offer, price_per_person=Decimal("1199")), True)
    assert events == ["price_changed", "new_low"]
    message = NotificationMessage.from_notification(store.pending()[-1]).render()

    assert "NAJNIŻSZA CENA" in message
    assert "💰 1199 zł/os." in message
    assert "📉 Było 1299 zł/os. • spadek 100 zł" in message


def test_pipeline_persists_final_score_for_stable_retries(
    offer: Offer, store: Store, settings: Settings
) -> None:
    pipeline = OfferPipeline(settings, store)
    result = pipeline.finalize([offer])[0]
    notification = store.pending()[0]
    original = NotificationMessage.from_notification(notification)

    store.observe(replace(result, final_score=999), True)
    retried = NotificationMessage.from_notification(store.pending()[0])

    assert result.final_score == pytest.approx(score(offer, settings.ranking, "1500"))
    assert original.offer.provider_rating_max == 10
    assert retried.render() == original.render()
    assert retried.offer.final_score == result.final_score


def test_incomplete_price_gets_a_short_disclaimer_under_the_link(
    offer: Offer, store: Store
) -> None:
    # A listing-only price (e.g. Wakacje.pl) reaching the outbox at all already
    # means filtering.matches() accepted it via the provider whitelist;
    # rendering must still disclose it is unconfirmed, kept short and below
    # the link, never mixed into the main body.
    candidate = replace(offer, provider="wakacje.pl", price_is_complete=False)
    store.observe(candidate, True)

    message = NotificationMessage.from_notification(store.pending()[0]).render()

    assert "ℹ️ Cena z listingu — niepotwierdzona." in message
    link_index = message.index("Zobacz ofertę")
    disclaimer_index = message.index("niepotwierdzona")
    assert disclaimer_index > link_index


def test_complete_price_has_no_disclaimer(offer: Offer, store: Store) -> None:
    # The default fixture offer already has price_is_complete=True.
    store.observe(offer, True)

    message = NotificationMessage.from_notification(store.pending()[0]).render()

    assert "niepotwierdzona" not in message


def test_confirmed_booking_total_is_shown_compactly(offer: Offer, store: Store) -> None:
    candidate = replace(
        offer,
        booking_total_price=Decimal("2700"),
        package_price=Decimal("2600"),
        operator_mandatory_fees=[OperatorFee("wiza", Decimal("100"), "PLN")],
        price_per_person=Decimal("1350"),
        total_price=Decimal("2700"),
        variant_verified=True,
        sale_status="available",
    )
    store.observe(candidate, True)

    message = NotificationMessage.from_notification(store.pending()[0]).render()

    assert "🧾 Cena całkowita rezerwacji: 2700 zł (zawiera opłaty obowiązkowe)" in message


def test_local_mandatory_costs_shown_only_when_present(offer: Offer, store: Store) -> None:
    store.observe(offer, True)
    baseline = NotificationMessage.from_notification(store.pending()[0]).render()
    assert "🧾" not in baseline

    candidate = replace(
        offer,
        hotel_name="Sunny Demo With Local Cost",
        local_mandatory_costs=[
            LocalMandatoryCost("Taksa klimatyczna", "exact", Decimal("15"), "PLN", "doba")
        ],
    )
    store.observe(candidate, True)
    with_cost = NotificationMessage.from_notification(store.pending()[-1]).render()
    assert "🧾 Taksa klimatyczna (15 PLN)" in with_cost


@pytest.mark.parametrize(
    ("departure", "returning"),
    [
        (date(2027, 9, 10), date(2027, 9, 17)),  # Meridian, manually verified live
        (date(2026, 10, 13), date(2026, 10, 20)),  # Flegra Palace, manually verified live
        (date(2027, 1, 9), date(2027, 1, 16)),  # Pebbles Resort, manually verified live
    ],
)
def test_stay_length_matches_wakacje_pl_own_days_and_nights_wording(
    offer: Offer, store: Store, departure: date, returning: date
) -> None:
    # Wakacje.pl's own offer pages show these exact three date spans (7 nights
    # apart) as "8 dni / 7 nocy", never as "7 dni" -- the site counts the
    # checkout day into "dni", a convention `Offer.number_of_days` does not
    # reliably carry for every provider (see notification_content._stay_length).
    candidate = replace(
        offer,
        provider="wakacje.pl",
        departure_date=departure,
        return_date=returning,
        number_of_days=7,
        price_is_complete=False,
    )
    store.observe(candidate, True)

    message = NotificationMessage.from_notification(store.pending()[0]).render()

    assert "8 dni / 7 nocy" in message
    assert "7 dni" not in message


@pytest.mark.parametrize(
    ("nights", "expected"),
    [
        (1, "2 dni / 1 noc"),
        (2, "3 dni / 2 noce"),
        (5, "6 dni / 5 nocy"),
    ],
)
def test_stay_length_polish_plural_forms(
    offer: Offer, store: Store, nights: int, expected: str
) -> None:
    departure = date(2027, 3, 1)
    candidate = replace(
        offer,
        departure_date=departure,
        return_date=departure + timedelta(days=nights),
    )
    store.observe(candidate, True)

    message = NotificationMessage.from_notification(store.pending()[0]).render()

    assert expected in message


def test_stay_length_is_derived_from_dates_not_the_raw_duration_field(
    offer: Offer, store: Store
) -> None:
    # A provider's own `number_of_days` meaning is not consistent (nights vs.
    # nights + 1) -- the departure/return dates are the one value every
    # provider agrees on, so the display must ignore a mismatched raw field.
    candidate = replace(
        offer,
        departure_date=date(2027, 9, 10),
        return_date=date(2027, 9, 17),
        number_of_days=999,
    )
    store.observe(candidate, True)

    message = NotificationMessage.from_notification(store.pending()[0]).render()

    assert "8 dni / 7 nocy" in message
    assert "999" not in message


def test_console_notifier_logs_full_message_once(
    offer: Offer, store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    store.observe(offer, True)

    with caplog.at_level(logging.INFO, logger="travel_deal_agent.notifications"):
        deliver_pending(store, ConsoleNotifier())
        deliver_pending(store, ConsoleNotifier())

    assert caplog.text.count("Sunny Demo") == 1
    assert "💰 1299 zł/os. (2598 zł / 2 osoby)" in caplog.text
    assert store.pending() == []
