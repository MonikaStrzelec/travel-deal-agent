from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from travel_deal_agent.config import Settings
from travel_deal_agent.config_types import RatingScale
from travel_deal_agent.filtering import matches
from travel_deal_agent.models import Offer
from travel_deal_agent.notifications import LogNotifier
from travel_deal_agent.providers.base import Provider
from travel_deal_agent.providers.external_rating import ExternalHotelRatingProvider, HotelRating
from travel_deal_agent.ranking import score
from travel_deal_agent.ratings import normalize_rating, validate_rating_rules
from travel_deal_agent.scheduler import Scheduler
from travel_deal_agent.storage import Store


@pytest.mark.parametrize(
    "provider,price,rating,expected",
    [
        ("rainbow", "999.99", 4.0, False),
        ("rainbow", "999.99", 5.0, True),
        ("rainbow", "999.99", 3.99, False),
        ("rainbow", "1000", 5.0, True),
        ("rainbow", "1000", 4.99, False),
        # Wakacje.pl: one price-independent threshold of 8.0 (no price bands).
        ("wakacje.pl", "500", 7.9, False),
        ("wakacje.pl", "500", 8.0, True),
        ("wakacje.pl", "999.99", 7.0, False),
        ("wakacje.pl", "999.99", 7.9, False),
        ("wakacje.pl", "999.99", 8.0, True),
        ("wakacje.pl", "1000", 8.0, True),
        ("wakacje.pl", "1000", 7.99, False),
        ("wakacje.pl", "1200", 7.9, False),
        ("wakacje.pl", "1200", 8.6, True),
        ("rainbow", "1499.99", 5.0, True),
        ("wakacje.pl", "1499.99", 8.0, True),
        ("rainbow", "1500", 5, True),
        ("wakacje.pl", "1500", 8, True),
        ("rainbow", "1500.01", 6, False),
        ("wakacje.pl", "1500.01", 10, False),
        ("rainbow", "999", None, False),
        ("wakacje.pl", "999", None, False),
        ("rainbow", "999", 6.1, False),
        ("wakacje.pl", "999", 10.1, False),
        ("rainbow", "999", 0.5, False),
        ("itaka", "999", None, False),
        ("itaka", "999.99", 4.0, True),
        ("itaka", "999.99", 3.99, False),
        ("itaka", "1000", 5.0, True),
        ("itaka", "1000", 4.99, False),
        ("itaka", "1500", 5.0, True),
        ("itaka", "1500", 4.99, False),
        ("itaka", "1500.01", 6, False),
        ("itaka", "999", 6.1, False),
        ("unknown", "999", 9, False),
        # MVP decision: no TUI hard rating filter; use rating/review data in
        # ranking. config.py defaults a registered-but-unconfigured provider to
        # a disabled rule, so the rating gate is a no-op -- unlike "unknown"
        # above, which is not a registered provider at all and stays fail-closed.
        ("tui", "999", None, True),
        ("tui", "1500", 1.0, True),
    ],
)
def test_provider_price_rating_rules(
    offer: Offer,
    settings: Settings,
    provider: str,
    price: str,
    rating: float | None,
    expected: bool,
) -> None:
    candidate = replace(offer, provider=provider, price_per_person=Decimal(price), rating=rating)
    assert matches(candidate, settings.filters) is expected


@pytest.mark.parametrize("minimum,maximum,midpoint", [(0, 6, 3), (0, 10, 5), (1, 5, 3)])
def test_normalization(minimum: float, maximum: float, midpoint: float) -> None:
    scale: RatingScale = {"min": minimum, "max": maximum}
    assert normalize_rating(minimum, scale) == 0
    assert normalize_rating(maximum, scale) == 100
    assert normalize_rating(midpoint, scale) == 50
    assert normalize_rating(None, scale) is None
    assert normalize_rating(maximum + 1, scale) is None
    assert normalize_rating(float("nan"), scale) is None
    assert normalize_rating(midpoint, None) is None


def test_additional_bands_without_logic_changes(offer: Offer, settings: Settings) -> None:
    filters = deepcopy(settings.filters)
    filters["provider_ratings"]["rainbow"]["price_bands"] = [
        {"min_price": "0", "max_price": "800", "min_rating": 4},
        {"min_price": "800", "max_price": "1000", "min_rating": 4.5},
        {"min_price": "1000", "max_price": "1250", "min_rating": 5},
        {"min_price": "1250", "max_price": "1500", "min_rating": 5.5},
    ]
    validate_rating_rules(filters["provider_ratings"])
    for price, expected in [("799", True), ("800", False), ("999", False)]:
        assert (
            matches(
                replace(offer, provider="rainbow", price_per_person=Decimal(price), rating=4),
                filters,
            )
            is expected
        )
    assert not matches(replace(offer, provider="rainbow", rating=5), filters)


@pytest.mark.parametrize("mutation", ["overlap", "scale", "threshold", "empty", "nan"])
def test_invalid_rules(settings: Settings, mutation: str) -> None:
    rules = deepcopy(settings.filters["provider_ratings"])
    rule = rules["rainbow"]
    if mutation == "overlap":
        rule["price_bands"][1]["min_price"] = "900"
    elif mutation == "scale":
        assert rule["scale"] is not None
        rule["scale"]["max"] = 0
    elif mutation == "threshold":
        rule["price_bands"][0]["min_rating"] = 7
    elif mutation == "empty":
        rule["price_bands"] = []
    else:
        rule["price_bands"][0]["max_price"] = "NaN"
    with pytest.raises(ValueError):
        validate_rating_rules(rules)


def test_wakacje_uses_one_configurable_threshold_without_price_bands(
    offer: Offer, settings: Settings
) -> None:
    # Arrange
    rule = settings.filters["provider_ratings"]["wakacje.pl"]
    filters = deepcopy(settings.filters)
    filters["provider_ratings"]["wakacje.pl"]["min_rating"] = 7.5
    candidate = replace(offer, provider="wakacje.pl", rating=7.6)
    # Act / Assert
    assert rule.get("min_rating") == 8
    assert rule["price_bands"] == []
    assert not matches(candidate, settings.filters)
    assert matches(candidate, filters)


@pytest.mark.parametrize("mutation", ["both", "out_of_scale", "missing"])
def test_invalid_single_threshold_rules(settings: Settings, mutation: str) -> None:
    # Arrange
    rules = deepcopy(settings.filters["provider_ratings"])
    rule = rules["wakacje.pl"]
    if mutation == "both":
        rule["price_bands"] = [{"min_price": "0", "max_price": "1500", "min_rating": 8}]
    elif mutation == "out_of_scale":
        rule["min_rating"] = 11
    else:
        del rule["min_rating"]
    # Act / Assert
    with pytest.raises(ValueError):
        validate_rating_rules(rules)


def test_cross_scale_ranking_and_google(offer: Offer, settings: Settings) -> None:
    ranking = deepcopy(settings.ranking)
    ranking["weights"] = {key: 0 for key in ranking["weights"]}
    ranking["weights"]["rating"] = 1
    a = replace(offer, provider="rainbow", rating=4.8)
    b = replace(offer, provider="wakacje.pl", rating=8)
    assert score(a, ranking, "1500") == pytest.approx(score(b, ranking, "1500"))
    assert score(replace(a, provider="itaka"), ranking, "1500") == pytest.approx(0.76)
    ranking["weights"]["google_rating"] = 1
    ranking["weights"]["google_reviews"] = 1
    verified = replace(
        a, external_rating_status="verified", google_rating={"rating": 5, "number_of_reviews": 100}
    )
    assert score(verified, ranking, "1500") > score(a, ranking, "1500")


class FixtureExternal(ExternalHotelRatingProvider):
    def __init__(self, mode: str) -> None:
        self.calls: list[str] = []
        self.mode = mode

    def verify(self, offer: Offer) -> HotelRating | None:
        self.calls.append(offer.offer_id)
        if self.mode == "error":
            raise RuntimeError("Fixture error")
        if self.mode == "missing":
            return None
        return HotelRating(
            source="google",
            rating=4.5,
            scale_min=1,
            scale_max=5,
            number_of_reviews=200,
            matched_hotel_name=offer.hotel_name or "Fixture Hotel",
            country=offer.country,
            confidence=1,
            location=offer.destination,
            external_id="fixture-place",
        )


@pytest.mark.parametrize(
    "mode,enabled", [("ok", True), ("missing", True), ("error", True), ("ok", False)]
)
def test_second_stage_only_checks_top_eligible_offers(
    offer: Offer, settings: Settings, store: Store, mode: str, enabled: bool
) -> None:

    class FixtureProvider(Provider):
        name = "rainbow"

        def fetch(self) -> list[Offer]:
            valid = replace(offer, provider=self.name, rating=5.5)
            return [
                valid,
                replace(valid, offer_id="low-rating", rating=4),
                replace(valid, offer_id="expensive", price_per_person=Decimal("1600")),
                replace(valid, offer_id="airport", departure_airport="KRK"),
                replace(
                    valid,
                    offer_id="short",
                    return_date=valid.departure_date + timedelta(days=1),  # type: ignore
                ),
                replace(valid, offer_id="stars", hotel_stars=2),
                replace(
                    valid,
                    offer_id="runner-up",
                    hotel_name="Other",
                    price_per_person=Decimal("1400"),
                ),
            ]

    settings = replace(
        settings,
        providers={"rainbow": {"enabled": True, "interval_seconds": 600}},
        external_verification={
            **settings.external_verification,
            "enabled": enabled,
            "max_candidates": 1,
        },
    )
    external = FixtureExternal(mode)
    scheduler = Scheduler(
        settings, [FixtureProvider()], store, LogNotifier(), external_provider=external
    )
    result = scheduler.run_once(force=True)
    assert len(result) == 2
    assert external.calls == ([offer.offer_id] if enabled else [])
    expected = "verified" if enabled and mode == "ok" else "external rating not verified"
    assert result[0].external_rating_status == expected
    stored = store.get_offer("rainbow", offer.offer_id)
    assert stored is not None
    assert stored.external_rating_status == expected
    assert result[1].external_rating_status == "external rating not verified"
