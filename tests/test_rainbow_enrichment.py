"""Offline checks for the shortlist and detail/listing join; no network access."""

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from travel_deal_agent.config import Settings
from travel_deal_agent.models import Offer
from travel_deal_agent.providers.rainbow_data import parse_card
from travel_deal_agent.providers.rainbow_details import (
    RainbowFlight,
    RainbowRoom,
    RainbowSelectedVariant,
)
from travel_deal_agent.providers.rainbow_enrichment import enrich_selected, potential_candidate
from travel_deal_agent.providers.rainbow_listing_data import ListingEvidence
from travel_deal_agent.providers.rainbow_nuxt import RainbowDetailError

HTML = (Path(__file__).parent / "fixtures/rainbow/card.html").read_text(encoding="utf-8")
NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)
TODAY = NOW.date()


def base_offer() -> Offer:
    # A bare card observation: unverified, one raw airport/meal option each.
    html = HTML.replace(" <span>(+1)</span>", "").replace("1 551", "1 400")
    return parse_card(html, NOW)


def matching_offer() -> Offer:
    # Same card, at the price used by the default evidence()/variant() fixtures below.
    return parse_card(HTML.replace(" <span>(+1)</span>", ""), NOW)


def evidence(**overrides: object) -> ListingEvidence:
    base = ListingEvidence(
        product_key="6466_12682:249522:10474247",
        opaque_key="OPAQUE-1",
        url="https://r.pl/turcja-riwiera-wczasy/gardenia-hotel?unikalnyKluczOferty=OPAQUE-1"
        "&liczbaPokoi=1&czyCenaZaWszystkich=0&wiek=1990-01-01&wiek=1992-06-15",
        birth_dates=("1990-01-01", "1992-06-15"),
        departure_date=date(2026, 12, 5),
        days=8,
        nights=7,
        price=Decimal("1551"),
        airports=("KTW", "WAW"),
        boards=("HB", "AI"),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def variant(**overrides: object) -> RainbowSelectedVariant:
    outbound = RainbowFlight("KTW", "AYT", date(2026, 12, 5), date(2026, 12, 5), None, None)
    inbound = RainbowFlight("AYT", "KTW", date(2026, 12, 12), date(2026, 12, 12), None, None)
    base = RainbowSelectedVariant(
        product_key="6466_12682:249522:10474247",
        opaque_key="OPAQUE-1",
        outbound=outbound,
        inbound=inbound,
        board="HB",
        board_source="2-posilki",
        room=RainbowRoom(14641, 35, "Pokój economy", 1),
        adults=2,
        children=0,
        departure_date=date(2026, 12, 5),
        return_date=date(2026, 12, 12),
        days=8,
        nights=7,
        price_per_person=Decimal("1551"),
        total_price=Decimal("3102"),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_potential_candidate_requires_core_fields(settings: Settings) -> None:
    # Arrange.
    incomplete = replace(base_offer(), hotel_stars=None)

    # Act / assert.
    assert not potential_candidate(incomplete, settings.filters, TODAY)


def test_potential_candidate_rejects_price_above_cap(settings: Settings) -> None:
    # Arrange.
    offer = replace(
        base_offer(), price_per_person=Decimal("1500.01"), total_price=Decimal("3000.02")
    )

    # Act / assert.
    assert not potential_candidate(offer, settings.filters, TODAY)


def test_potential_candidate_true_for_a_single_known_option(settings: Settings) -> None:
    # Arrange: the card already resolved one airport/meal (no evidence yet).
    offer = base_offer()
    assert offer.departure_airport == "KTW" and offer.board_type == "HB"

    # Act / assert.
    assert potential_candidate(offer, settings.filters, TODAY)


def test_potential_candidate_uses_any_option_not_a_combination(settings: Settings) -> None:
    # Arrange: evidence lists an allowed and a disallowed airport/meal independently.
    offer = replace(base_offer(), departure_airport=None, board_type=None)
    mixed = evidence(airports=("KRK", "KTW"), boards=("BB", "AI"))

    # Act / assert: eligible because KTW/AI each independently qualify, without
    # requiring or assuming the specific KTW+AI combination was ever offered.
    assert potential_candidate(offer, settings.filters, TODAY, mixed)


def test_potential_candidate_false_when_no_option_qualifies(settings: Settings) -> None:
    # Arrange.
    offer = replace(base_offer(), departure_airport=None, board_type=None)
    unsupported = evidence(airports=("KRK",), boards=("BB", "AI"))

    # Act / assert.
    assert not potential_candidate(offer, settings.filters, TODAY, unsupported)


def test_enrich_selected_narrows_to_the_confirmed_configuration(settings: Settings) -> None:
    # Arrange.
    offer = replace(matching_offer(), departure_airport=None, board_type=None)
    e = evidence()
    v = variant()

    # Act.
    result = enrich_selected(offer, e, v)

    # Assert: exactly the confirmed configuration, not every listed option.
    assert result.departure_airport == "KTW"
    assert result.board_type == "HB"
    assert result.return_date == date(2026, 12, 12)
    assert result.total_price == Decimal("3102")
    assert result.url == e.url
    assert result.variant_verified is True
    # Price completeness is never claimed from CenaAvg/CenaSum alone.
    assert result.price_is_complete is False
    assert result.booking_total_price is None
    assert "mandatory costs" in (result.price_verification_reason or "")
    assert result.variant_identity is not None and result.variant_identity.startswith("rainbow-")


def test_enrich_selected_is_deterministic_and_stable_for_the_same_variant(
    settings: Settings,
) -> None:
    # Arrange.
    offer = matching_offer()
    e = evidence()

    # Act.
    first = enrich_selected(offer, e, variant())
    second = enrich_selected(offer, e, variant())

    # Assert.
    assert first.variant_identity == second.variant_identity


def test_enrich_selected_rejects_price_mismatch_with_listing(settings: Settings) -> None:
    # Arrange: never assign the listing price to an unconfirmed alternative price.
    offer = matching_offer()
    e = evidence()
    mismatched = variant(price_per_person=Decimal("1602"), total_price=Decimal("3204"))

    # Act / assert.
    with pytest.raises(RainbowDetailError):
        enrich_selected(offer, e, mismatched)


def test_enrich_selected_rejects_airport_outside_evidence_options(settings: Settings) -> None:
    # Arrange: the selected flight airport must be one of the listing's own options.
    offer = matching_offer()
    e = evidence(airports=("WAW",))  # KTW no longer a listed option
    v = variant()

    # Act / assert.
    with pytest.raises(RainbowDetailError):
        enrich_selected(offer, e, v)


def test_enrich_selected_rejects_conflicting_card_airport(settings: Settings) -> None:
    # Arrange: the card already showed a specific airport that disagrees with the variant.
    offer = replace(matching_offer(), departure_airport="WAW")
    e = evidence()
    v = variant()

    # Act / assert.
    with pytest.raises(RainbowDetailError):
        enrich_selected(offer, e, v)
