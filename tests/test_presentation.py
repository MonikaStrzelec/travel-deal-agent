from dataclasses import replace

import pytest

from travel_deal_agent.config import Settings
from travel_deal_agent.config_types import RatingRule
from travel_deal_agent.models import Offer
from travel_deal_agent.presentation import format_ratings
from travel_deal_agent.ratings import validate_rating_rules


def test_native_and_google_display(offer: Offer, settings: Settings) -> None:
    candidate = replace(
        offer,
        provider="itaka",
        rating=5.3,
        external_rating_status="verified",
        google_rating={"rating": 4.4},
    )
    assert (
        format_ratings(candidate, settings.ranking)
        == "ITAKA: 5.3/6 | Google: 4.4/5 | Tripadvisor: brak danych"
    )
    assert (
        format_ratings(replace(candidate, google_rating=None), settings.ranking)
        == "ITAKA: 5.3/6 | Google: brak danych | Tripadvisor: brak danych"
    )
    assert (
        format_ratings(
            replace(candidate, external_rating_status="external rating not verified"),
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
