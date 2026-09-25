"""Attractiveness classification (V0): presentation-only HOT/GOOD/MATCH,
independent of ranking.score/Offer.final_score and of filtering.matches()."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from travel_deal_agent.attractiveness import (
    DEFAULT_ATTRACTIVENESS_CONFIG,
    classify_airport,
    classify_board,
    classify_hotel_quality,
    classify_offer,
    classify_value,
    validate_attractiveness_config,
)
from travel_deal_agent.config import Settings
from travel_deal_agent.config_types import AttractivenessConfig, RatingRule
from travel_deal_agent.filtering import matches
from travel_deal_agent.models import Offer

WAKACJE_RULE: RatingRule = {"enabled": True, "scale": {"min": 0, "max": 10}, "price_bands": []}
ITAKA_RULE: RatingRule = {"enabled": True, "scale": {"min": 1, "max": 6}, "price_bands": []}


# --- VALUE -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("price_per_person", "nights", "expected"),
    [
        (Decimal("1120"), 7, "strong"),  # 160 PLN/night
        (Decimal("1400"), 7, "normal"),  # 200 PLN/night
        (Decimal("1610"), 7, "weak"),  # 230 PLN/night
    ],
)
def test_value_thresholds(price_per_person: Decimal, nights: int, expected: str) -> None:
    assert classify_value(price_per_person, nights, DEFAULT_ATTRACTIVENESS_CONFIG) == expected


@pytest.mark.parametrize(
    ("price_per_person", "nights"),
    [(Decimal("1000"), 0), (Decimal("1000"), None), (None, 7), (None, None)],
)
def test_value_fails_safe_instead_of_dividing_by_zero(
    price_per_person: Decimal | None, nights: int | None
) -> None:
    assert classify_value(price_per_person, nights, DEFAULT_ATTRACTIVENESS_CONFIG) == "weak"


# --- HOTEL QUALITY -------------------------------------------------------------


@pytest.mark.parametrize("rating,expected", [(8.6, "strong"), (8.0, "normal")])
def test_hotel_quality_wakacje_scale(offer: Offer, rating: float, expected: str) -> None:
    candidate = replace(offer, provider="wakacje.pl", rating=rating, hotel_stars=4)
    assert (
        classify_hotel_quality(
            candidate, {"wakacje.pl": WAKACJE_RULE}, DEFAULT_ATTRACTIVENESS_CONFIG
        )
        == expected
    )


def test_hotel_quality_itaka_scale_normalizes_correctly(offer: Offer) -> None:
    # 5.3 on ITAKA's native 1-6 scale is (5.3-1)/(6-1) = 86%, the same "strong"
    # band as 8.6/10 on Wakacje.pl's scale -- normalization must not depend on
    # a hardcoded 0-10 assumption.
    candidate = replace(offer, provider="itaka", rating=5.3, hotel_stars=3)
    assert (
        classify_hotel_quality(candidate, {"itaka": ITAKA_RULE}, DEFAULT_ATTRACTIVENESS_CONFIG)
        == "strong"
    )


def test_hotel_quality_tui_scale_normalizes_correctly(offer: Offer) -> None:
    # TUI's only rating is TripAdvisor, confirmed 1-5 by captured production
    # data (tui_data.normalize_offer clamps to this range). The hard rating
    # filter stays disabled (MVP decision, see provider_ratings.tui in
    # config.json), but the scale itself lets normalize_rating -- and so
    # attractiveness/ranking -- read a real TUI rating instead of treating it
    # as unscaled and always falling back to "weak".
    tui_rule: RatingRule = {"enabled": False, "scale": {"min": 1, "max": 5}, "price_bands": []}
    candidate = replace(offer, provider="tui", rating=4.4, hotel_stars=4)  # (4.4-1)/(5-1)=85%
    assert (
        classify_hotel_quality(candidate, {"tui": tui_rule}, DEFAULT_ATTRACTIVENESS_CONFIG)
        == "strong"
    )


def test_five_stars_can_raise_normal_to_strong(offer: Offer) -> None:
    rule = {"wakacje.pl": WAKACJE_RULE}
    base = replace(offer, provider="wakacje.pl", rating=8.0, hotel_stars=3)
    assert classify_hotel_quality(base, rule, DEFAULT_ATTRACTIVENESS_CONFIG) == "normal"
    assert (
        classify_hotel_quality(replace(base, hotel_stars=5), rule, DEFAULT_ATTRACTIVENESS_CONFIG)
        == "strong"
    )


def test_four_stars_can_raise_weak_to_normal_but_three_cannot(offer: Offer) -> None:
    rule = {"wakacje.pl": WAKACJE_RULE}
    base = replace(offer, provider="wakacje.pl", rating=6.0, hotel_stars=3)  # 60% -> weak
    assert classify_hotel_quality(base, rule, DEFAULT_ATTRACTIVENESS_CONFIG) == "weak"
    assert (
        classify_hotel_quality(replace(base, hotel_stars=4), rule, DEFAULT_ATTRACTIVENESS_CONFIG)
        == "normal"
    )


def test_missing_reviews_never_lowers_hotel_quality(offer: Offer) -> None:
    rule = {"wakacje.pl": WAKACJE_RULE}
    with_reviews = replace(
        offer, provider="wakacje.pl", rating=8.0, hotel_stars=4, number_of_reviews=500
    )
    without_reviews = replace(with_reviews, number_of_reviews=None)
    zero_reviews = replace(with_reviews, number_of_reviews=0)
    levels = {
        classify_hotel_quality(candidate, rule, DEFAULT_ATTRACTIVENESS_CONFIG)
        for candidate in (with_reviews, without_reviews, zero_reviews)
    }
    assert levels == {"normal"}


def test_missing_google_or_tripadvisor_never_affects_hotel_quality(offer: Offer) -> None:
    rule = {"wakacje.pl": WAKACJE_RULE}
    base = replace(offer, provider="wakacje.pl", rating=8.0, hotel_stars=4)
    with_google = replace(
        base,
        google_rating={"rating": 4.9},
        google_rating_max=5,
        external_rating_status="verified",
    )
    assert classify_hotel_quality(
        base, rule, DEFAULT_ATTRACTIVENESS_CONFIG
    ) == classify_hotel_quality(with_google, rule, DEFAULT_ATTRACTIVENESS_CONFIG)


# --- AIRPORT / BOARD -----------------------------------------------------------


@pytest.mark.parametrize(
    ("airport", "expected"),
    [
        ("LCJ", "strong"),
        ("WAW", "normal"),
        ("WMI", "normal"),
        ("KTW", "neutral"),
        ("WRO", "neutral"),
    ],
)
def test_airport_tiers(airport: str, expected: str) -> None:
    assert classify_airport(airport, DEFAULT_ATTRACTIVENESS_CONFIG) == expected


@pytest.mark.parametrize(
    ("board", "expected"),
    [
        ("AI", "strong"),
        ("UAI", "strong"),
        ("FB", "normal"),
        ("HB", "normal"),
        ("ZO", "neutral"),
    ],
)
def test_board_tiers(board: str, expected: str) -> None:
    assert classify_board(board, DEFAULT_ATTRACTIVENESS_CONFIG) == expected


# --- FINAL CATEGORY --------------------------------------------------------------


def _at_nights(offer: Offer, nights: int) -> Offer:
    assert offer.departure_date is not None
    return replace(offer, return_date=offer.departure_date + timedelta(days=nights))


def test_two_strong_and_no_weak_is_hot(offer: Offer) -> None:
    candidate = _at_nights(
        replace(
            offer,
            provider="wakacje.pl",
            rating=9.0,
            hotel_stars=4,
            departure_airport="WAW",
            board_type="HB",
            price_per_person=Decimal("1000"),
        ),
        6,
    )  # 166.7 PLN/night -> strong VALUE; rating 90% -> strong HOTEL QUALITY
    breakdown = classify_offer(
        candidate, {"wakacje.pl": WAKACJE_RULE}, DEFAULT_ATTRACTIVENESS_CONFIG
    )
    assert breakdown.value == "strong"
    assert breakdown.hotel_quality == "strong"
    assert breakdown.category == "HOT"


def test_one_strong_with_at_most_one_weak_is_good(offer: Offer) -> None:
    candidate = _at_nights(
        replace(
            offer,
            provider="wakacje.pl",
            rating=9.0,
            hotel_stars=4,
            departure_airport="KTW",
            board_type="HB",
            price_per_person=Decimal("1200"),
        ),
        6,
    )  # 200 PLN/night -> normal VALUE; rating 90% -> strong HOTEL QUALITY; neutral airport
    breakdown = classify_offer(
        candidate, {"wakacje.pl": WAKACJE_RULE}, DEFAULT_ATTRACTIVENESS_CONFIG
    )
    assert breakdown.category == "GOOD"


def test_no_strong_area_is_match(offer: Offer) -> None:
    candidate = _at_nights(
        replace(
            offer,
            provider="wakacje.pl",
            rating=8.0,
            hotel_stars=3,
            departure_airport="KTW",
            board_type="HB",
            price_per_person=Decimal("1200"),
        ),
        6,
    )  # normal VALUE, normal HOTEL QUALITY, neutral airport, normal board -- no strong area
    breakdown = classify_offer(
        candidate, {"wakacje.pl": WAKACJE_RULE}, DEFAULT_ATTRACTIVENESS_CONFIG
    )
    assert breakdown.category == "MATCH"


def test_lcj_alone_does_not_give_hot(offer: Offer) -> None:
    candidate = _at_nights(
        replace(
            offer,
            provider="wakacje.pl",
            rating=8.0,
            hotel_stars=3,
            departure_airport="LCJ",
            board_type="HB",
            price_per_person=Decimal("1200"),
        ),
        6,
    )  # only AIRPORT is strong; everything else normal/neutral
    breakdown = classify_offer(
        candidate, {"wakacje.pl": WAKACJE_RULE}, DEFAULT_ATTRACTIVENESS_CONFIG
    )
    assert breakdown.airport == "strong"
    assert breakdown.category != "HOT"


def test_a_great_offer_from_ktw_can_be_hot_without_lcj(offer: Offer) -> None:
    candidate = _at_nights(
        replace(
            offer,
            provider="wakacje.pl",
            rating=9.5,
            hotel_stars=4,
            departure_airport="KTW",
            board_type="AI",
            price_per_person=Decimal("1000"),
        ),
        6,
    )  # strong VALUE + strong HOTEL QUALITY + strong BOARD; airport merely neutral
    breakdown = classify_offer(
        candidate, {"wakacje.pl": WAKACJE_RULE}, DEFAULT_ATTRACTIVENESS_CONFIG
    )
    assert breakdown.airport == "neutral"
    assert breakdown.category == "HOT"


def test_classification_never_changes_filtering_matches(offer: Offer, settings: Settings) -> None:
    before = matches(offer, settings.filters)
    classify_offer(offer, settings.filters["provider_ratings"], settings.attractiveness)
    after = matches(offer, settings.filters)
    assert before == after


# --- CONFIG VALIDATION -----------------------------------------------------------


def test_default_config_is_valid() -> None:
    validate_attractiveness_config(DEFAULT_ATTRACTIVENESS_CONFIG)


def test_value_strong_threshold_must_be_below_normal() -> None:
    bad: AttractivenessConfig = {
        **DEFAULT_ATTRACTIVENESS_CONFIG,
        "value": {
            "strong_max_price_per_person_per_night": "220",
            "normal_max_price_per_person_per_night": "170",
        },
    }
    with pytest.raises(ValueError):
        validate_attractiveness_config(bad)


def test_hotel_quality_rating_must_be_in_unit_range() -> None:
    bad: AttractivenessConfig = {
        **DEFAULT_ATTRACTIVENESS_CONFIG,
        "hotel_quality": {
            "strong_min_normalized_rating": 1.5,
            "normal_min_normalized_rating": 0.75,
        },
    }
    with pytest.raises(ValueError):
        validate_attractiveness_config(bad)


def test_hotel_quality_strong_must_be_at_least_normal() -> None:
    bad: AttractivenessConfig = {
        **DEFAULT_ATTRACTIVENESS_CONFIG,
        "hotel_quality": {
            "strong_min_normalized_rating": 0.6,
            "normal_min_normalized_rating": 0.75,
        },
    }
    with pytest.raises(ValueError):
        validate_attractiveness_config(bad)
