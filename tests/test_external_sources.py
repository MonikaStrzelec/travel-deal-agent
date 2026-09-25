"""Independent external sources, conservative matching and bounded enrichment."""

from dataclasses import replace
from decimal import Decimal

import pytest

from conftest import TEST_CONFIG
from travel_deal_agent.config import Settings
from travel_deal_agent.models import ExternalHotelRating, Offer
from travel_deal_agent.notification_content import NotificationMessage
from travel_deal_agent.pipeline import OfferPipeline
from travel_deal_agent.presentation import format_ratings
from travel_deal_agent.providers.external_rating import (
    ExternalHotelRatingProvider,
    GoogleRatingProvider,
    TripadvisorRatingProvider,
    verify_external,
)
from travel_deal_agent.ranking import score
from travel_deal_agent.storage import Store


class FixtureRatingProvider(ExternalHotelRatingProvider):
    def __init__(self, source: str, result: ExternalHotelRating | None, fail: bool = False) -> None:
        self.source = source
        self.result = result
        self.fail = fail
        self.calls: list[Offer] = []

    def verify(self, offer: Offer) -> ExternalHotelRating | None:
        self.calls.append(offer)
        if self.fail:
            raise RuntimeError("Synthetic source failure")
        return self.result


@pytest.fixture
def evidence(offer: Offer) -> ExternalHotelRating:
    return ExternalHotelRating(
        "google",
        4.4,
        1,
        5,
        123,
        offer.hotel_name or "",
        offer.country,
        0.95,
        offer.destination,
        "fixture-id",
    )


def test_two_sources_persist_and_render(
    offer: Offer, evidence: ExternalHotelRating, settings: Settings, store: Store
) -> None:
    # Arrange
    settings.external_verification["enabled"] = True
    adapters = [
        FixtureRatingProvider("google", evidence),
        FixtureRatingProvider("tripadvisor", replace(evidence, source="tripadvisor", rating=4.5)),
    ]
    pipeline = OfferPipeline(settings, store, external_providers=adapters)
    # Act
    result = pipeline.finalize(pipeline.filter_batch(offer.provider, [offer]))[0]
    stored = store.get_offer(offer.provider, offer.offer_id)
    message = NotificationMessage.from_notification(store.pending()[0])
    # Assert
    assert stored is not None
    assert stored.hotel_ratings == result.hotel_ratings == message.offer.hotel_ratings
    assert stored.hotel_ratings["google"].external_id == "fixture-id"
    assert "Google: 4.4/5 | Tripadvisor: 4.5/5" in format_ratings(result, settings.ranking)
    assert "(Google: 4,4/5)" in message.render()
    assert "(TripAdvisor: 4,5/5)" in message.render()
    assert score(result, settings.ranking, "1500") > score(offer, settings.ranking, "1500")


@pytest.mark.parametrize(
    "change",
    ["country", "region", "missing_region", "name", "confidence", "ambiguous", "source", "scale"],
)
def test_untrusted_matching_rejected(
    offer: Offer, evidence: ExternalHotelRating, settings: Settings, change: str
) -> None:
    variants = {
        "country": replace(evidence, country="ES"),
        "region": replace(evidence, location="Other"),
        "missing_region": replace(evidence, location=None),
        "name": replace(evidence, matched_hotel_name="Other"),
        "confidence": replace(evidence, confidence=0.89),
        "ambiguous": replace(evidence, ambiguous=True),
        "source": replace(evidence, source="tripadvisor"),
        "scale": replace(evidence, scale_max=10),
    }
    settings.external_verification["enabled"] = True
    result = verify_external(
        offer, FixtureRatingProvider("google", variants[change]), settings.external_verification
    )
    assert result.hotel_ratings == {}
    assert result.external_verification_statuses["google"] == "rejected_match"


@pytest.mark.parametrize("missing", ["name", "country"])
def test_incomplete_identity_skips_provider(
    offer: Offer, evidence: ExternalHotelRating, settings: Settings, missing: str
) -> None:
    adapter = FixtureRatingProvider("google", evidence)
    settings.external_verification["enabled"] = True
    candidate = (
        replace(offer, hotel_name=None) if missing == "name" else replace(offer, country=None)
    )
    result = verify_external(candidate, adapter, settings.external_verification)
    assert not adapter.calls
    assert not result.hotel_ratings


def test_optional_region_and_normalized_name(
    offer: Offer, evidence: ExternalHotelRating, settings: Settings
) -> None:
    settings.external_verification["enabled"] = True
    candidate = replace(offer, destination=None)
    adapter = FixtureRatingProvider(
        "google", replace(evidence, matched_hotel_name="  SUNNY   DEMO ", confidence=0.9)
    )
    assert verify_external(candidate, adapter, settings.external_verification).hotel_ratings


@pytest.mark.parametrize("mode", ["error", "missing", "disabled"])
def test_failure_isolation(
    offer: Offer, evidence: ExternalHotelRating, settings: Settings, mode: str
) -> None:
    settings.external_verification["enabled"] = True
    settings.external_verification["sources"]["google"]["enabled"] = mode != "disabled"
    google = FixtureRatingProvider("google", None, fail=mode == "error")
    tripadvisor = FixtureRatingProvider("tripadvisor", replace(evidence, source="tripadvisor"))
    result = verify_external(offer, [google, tripadvisor], settings.external_verification)
    assert set(result.hotel_ratings) == {"tripadvisor"}
    assert len(google.calls) == (0 if mode == "disabled" else 1)
    assert "Google: brak danych | Tripadvisor: 4.4/5" in format_ratings(result, settings.ranking)


@pytest.mark.parametrize(
    "limit,enabled,expected", [(0, True, 0), (1, True, 1), (2, True, 2), (2, False, 0)]
)
def test_order_deduplication_and_candidate_budget(
    offer: Offer,
    evidence: ExternalHotelRating,
    settings: Settings,
    store: Store,
    limit: int,
    enabled: bool,
    expected: int,
) -> None:
    # Arrange
    settings.external_verification["enabled"] = enabled
    settings.external_verification["max_candidates"] = limit
    adapters = [
        FixtureRatingProvider("google", evidence),
        FixtureRatingProvider("tripadvisor", replace(evidence, source="tripadvisor")),
    ]
    runner = replace(
        offer, offer_id="runner", hotel_name="Runner", price_per_person=Decimal("1490")
    )
    duplicate = replace(offer, offer_id="duplicate", price_per_person=Decimal("1300"))
    rejected = replace(offer, offer_id="rejected", hotel_stars=1)
    pipeline = OfferPipeline(settings, store, external_providers=adapters)
    # Act
    results = pipeline.finalize(
        pipeline.filter_batch(offer.provider, [runner, rejected, duplicate, offer, offer])
    )
    # Assert
    assert len(results) == 2
    for adapter in adapters:
        assert len(adapter.calls) == expected
        assert [o.offer_id for o in adapter.calls] == [offer.offer_id, "runner"][:expected]
        assert all(not o.hotel_ratings for o in adapter.calls)


def test_placeholder_providers_are_offline(offer: Offer) -> None:
    assert GoogleRatingProvider().verify(offer) is None
    assert TripadvisorRatingProvider().verify(offer) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("rating", float("nan")),
        ("rating", 6),
        ("scale_max", 1),
        ("confidence", 1.1),
        ("number_of_reviews", -1),
    ],
)
def test_invalid_evidence(evidence: ExternalHotelRating, field: str, value: float) -> None:
    with pytest.raises(ValueError):
        if field == "rating":
            replace(evidence, rating=value)
        elif field == "scale_max":
            replace(evidence, scale_max=value)
        elif field == "confidence":
            replace(evidence, confidence=value)
        else:
            replace(evidence, number_of_reviews=int(value))


def test_duplicate_registration_rejected(settings: Settings, store: Store) -> None:
    with pytest.raises(ValueError, match="unique"):
        OfferPipeline(
            settings, store, external_providers=[GoogleRatingProvider(), GoogleRatingProvider()]
        )


@pytest.mark.parametrize("mutation", ["confidence", "scale", "weight"])
def test_invalid_source_configuration(mutation: str) -> None:
    from pydantic import TypeAdapter

    from travel_deal_agent.config import validate_options
    from travel_deal_agent.config_types import AppConfig

    # Arrange
    config = TypeAdapter(AppConfig).validate_json(TEST_CONFIG.read_text(encoding="utf-8-sig"))
    policy = config["external_verification"]["sources"]["tripadvisor"]
    if mutation == "confidence":
        policy["min_confidence"] = 1.01
    elif mutation == "scale":
        policy["scale"]["max"] = policy["scale"]["min"]
    else:
        policy["rating_weight"] = -1
    # Act / Assert
    with pytest.raises(ValueError):
        validate_options(config)


def test_independent_scale_normalization(
    offer: Offer, evidence: ExternalHotelRating, settings: Settings
) -> None:
    settings.external_verification["enabled"] = True
    policy = settings.external_verification["sources"]["tripadvisor"]
    policy["scale"] = {"min": 0, "max": 10}
    policy["rating_weight"] = 2
    policy["reviews_weight"] = 0
    adapter = FixtureRatingProvider(
        "tripadvisor", replace(evidence, source="tripadvisor", rating=5, scale_min=0, scale_max=10)
    )
    result = verify_external(offer, adapter, settings.external_verification)
    assert score(result, settings.ranking, "1500") - score(
        offer, settings.ranking, "1500"
    ) == pytest.approx(1)
    assert "Tripadvisor: 5/10" in format_ratings(result, settings.ranking)
