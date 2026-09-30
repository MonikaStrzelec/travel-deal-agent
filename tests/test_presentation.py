from dataclasses import replace

import pytest

from travel_deal_agent.config import Settings
from travel_deal_agent.config_types import RatingRule
from travel_deal_agent.models import ExternalHotelRating, Offer
from travel_deal_agent.presentation import format_ratings
from travel_deal_agent.ratings import validate_rating_rules


def test_native_and_google_display(offer: Offer, settings: Settings) -> None:
    evidence = ExternalHotelRating(
        "google", 4.4, 1, 5, 100, offer.hotel_name or "", offer.country, 0.95, offer.destination
    )
    candidate = replace(
        offer,
        provider="itaka",
        rating=5.3,
        hotel_ratings={"google": evidence},
        external_verification_statuses={"google": "verified"},
    )
    assert (
        format_ratings(candidate, settings.ranking)
        == "ITAKA: 5.3/6 | Google: 4.4/5 | Tripadvisor: brak danych"
    )
    assert (
        format_ratings(replace(candidate, hotel_ratings={}), settings.ranking)
        == "ITAKA: 5.3/6 | Google: brak danych | Tripadvisor: brak danych"
    )
    assert (
        format_ratings(
            replace(candidate, external_verification_statuses={"google": "rejected_match"}),
            settings.ranking,
        )
        == "ITAKA: 5.3/6 | Google: brak danych | Tripadvisor: brak danych"
    )


def test_inclusive_bands_cannot_overlap_at_boundary() -> None:
    rule: RatingRule = {
        "enabled": True,
        "scale": {"min": 0, "max": 6},
        "price_bands": [
            {"min_price": "0", "max_price": "1000", "max_inclusive": True, "min_rating": 4},
            {"min_price": "1000", "max_price": "1500", "min_rating": 5},
        ],
    }
    with pytest.raises(ValueError, match="non-overlapping"):
        validate_rating_rules({"test": rule})
