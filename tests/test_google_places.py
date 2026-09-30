"""Google Places API (New) integration: HTTP always mocked, cache always local.

Covers `GoogleRatingProvider` (providers/external_rating.py) and its transport
(providers/google_places.py) end to end, including how `verify_external`
accepts or rejects what Google returns, and the SQLite-backed cache in
`storage.Store`. No test in this file performs a live request (see
conftest.no_network and conftest.no_google_api_key).
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from travel_deal_agent.config import Settings
from travel_deal_agent.models import ExternalHotelRating, Offer
from travel_deal_agent.providers.external_rating import GoogleRatingProvider, verify_external
from travel_deal_agent.providers.google_places import (
    GooglePlace,
    GooglePlacesError,
    UrllibGooglePlacesTransport,
)
from travel_deal_agent.storage import Store


class FakeTransport:
    """Records every query; returns `places` (or raises `error`) each call."""

    def __init__(
        self, places: list[GooglePlace] | None = None, error: Exception | None = None
    ) -> None:
        self.places = places or []
        self.error = error
        self.queries: list[str] = []

    def search_text(
        self, query: str, api_key: str, field_mask: str, timeout: float
    ) -> list[GooglePlace]:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.places


class MustNotBeCalledTransport:
    def search_text(
        self, query: str, api_key: str, field_mask: str, timeout: float
    ) -> list[GooglePlace]:
        raise AssertionError("Google must not be called")


def _place(
    name: str = "Sunny Demo",
    rating: float = 4.7,
    reviews: int | None = 12430,
    address: str = "Main Rd, Crete, Greece",
    place_id: str = "place-1",
) -> GooglePlace:
    return GooglePlace(
        place_id=place_id,
        display_name=name,
        rating=rating,
        user_rating_count=reviews,
        formatted_address=address,
    )


def _cached_evidence(offer: Offer, external_id: str = "cached-place") -> ExternalHotelRating:
    return ExternalHotelRating(
        source="google",
        rating=4.5,
        scale_min=1,
        scale_max=5,
        number_of_reviews=999,
        matched_hotel_name=offer.hotel_name or "",
        country=offer.country,
        confidence=0.97,
        location=offer.destination,
        external_id=external_id,
    )


@pytest.fixture
def google_settings(settings: Settings) -> Settings:
    settings.external_verification["enabled"] = True
    return settings


# --- 1. Successful match ------------------------------------------------------


def test_successful_match_yields_rating_reviews_and_place_id(
    offer: Offer, google_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    transport = FakeTransport([_place()])
    provider = GoogleRatingProvider(transport=transport)

    result = verify_external(offer, provider, google_settings.external_verification)

    evidence = result.hotel_ratings["google"]
    assert result.external_verification_statuses["google"] == "verified"
    assert evidence.rating == 4.7
    assert evidence.number_of_reviews == 12430
    assert evidence.external_id == "place-1"
    assert transport.queries == ["Sunny Demo, Crete, Greece"]


# --- 2. Missing API key --------------------------------------------------------


def test_missing_api_key_skips_without_crashing(offer: Offer, google_settings: Settings) -> None:
    # conftest.no_google_api_key already keeps GOOGLE_PLACES_API_KEY unset.
    provider = GoogleRatingProvider(transport=MustNotBeCalledTransport())

    result = verify_external(offer, provider, google_settings.external_verification)

    assert result.hotel_ratings == {}
    assert result.external_verification_statuses["google"] == "no_match"


# --- 3. HTTP error --------------------------------------------------------------


def test_http_error_is_isolated_as_a_source_error(
    offer: Offer, google_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    transport = FakeTransport(error=GooglePlacesError("Google Places API returned HTTP 500"))
    provider = GoogleRatingProvider(transport=transport)

    result = verify_external(offer, provider, google_settings.external_verification)

    assert result.hotel_ratings == {}
    assert result.external_verification_statuses["google"] == "error"


# --- 4. Timeout -----------------------------------------------------------------


def test_timeout_is_wrapped_as_a_google_places_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(*args: object, **kwargs: object) -> object:
        raise TimeoutError("timed out")

    monkeypatch.setattr("travel_deal_agent.providers.google_places.urlopen", raise_timeout)

    with pytest.raises(GooglePlacesError):
        UrllibGooglePlacesTransport().search_text("Sunny Demo, Greece", "key", "places.id", 5.0)


def test_timeout_at_the_provider_level_is_isolated_as_a_source_error(
    offer: Offer, google_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    transport = FakeTransport(error=GooglePlacesError("Google Places request failed: timed out"))
    provider = GoogleRatingProvider(transport=transport)

    result = verify_external(offer, provider, google_settings.external_verification)

    assert result.hotel_ratings == {}
    assert result.external_verification_statuses["google"] == "error"


# --- 5. Ambiguous result ---------------------------------------------------------


def test_ambiguous_result_is_rejected_not_guessed(
    offer: Offer, google_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    transport = FakeTransport(
        [
            _place(place_id="place-1", address="Main Rd, Crete, Greece"),
            _place(place_id="place-2", address="Side St, Crete, Greece"),
        ]
    )
    provider = GoogleRatingProvider(transport=transport)

    result = verify_external(offer, provider, google_settings.external_verification)

    assert result.hotel_ratings == {}
    assert result.external_verification_statuses["google"] == "rejected_match"


# --- 6. Hotel name mismatch -------------------------------------------------------


def test_hotel_name_mismatch_is_rejected(
    offer: Offer, google_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    transport = FakeTransport([_place(name="A Completely Different Resort")])
    provider = GoogleRatingProvider(transport=transport)

    result = verify_external(offer, provider, google_settings.external_verification)

    assert result.hotel_ratings == {}
    assert result.external_verification_statuses["google"] == "rejected_match"


# --- 7. Country mismatch -----------------------------------------------------------


def test_country_mismatch_is_rejected(
    offer: Offer, google_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    # offer.country is "GR" (Greece); Google reports a Turkish address instead.
    transport = FakeTransport([_place(address="Some Ave, Antalya, Turkey")])
    provider = GoogleRatingProvider(transport=transport)

    result = verify_external(offer, provider, google_settings.external_verification)

    assert result.hotel_ratings == {}
    assert result.external_verification_statuses["google"] == "rejected_match"


# --- 8/9. Cache freshness ------------------------------------------------------------


def test_fresh_cache_entry_avoids_a_request(offer: Offer, settings: Settings) -> None:
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    with Store(settings.database, clock=lambda: now[0]) as store:
        assert offer.hotel_name is not None and offer.country is not None
        store.save_cached_hotel_rating(
            "google", offer.hotel_name, offer.country, _cached_evidence(offer)
        )
        now[0] += timedelta(hours=1)  # still inside the 24h TTL
        provider = GoogleRatingProvider(
            store=store, transport=MustNotBeCalledTransport(), cache_ttl=timedelta(hours=24)
        )

        result = provider.verify(offer)

        assert result is not None
        assert result.external_id == "cached-place"


def test_expired_cache_entry_triggers_a_new_request(
    offer: Offer, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    with Store(settings.database, clock=lambda: now[0]) as store:
        assert offer.hotel_name is not None and offer.country is not None
        store.save_cached_hotel_rating(
            "google", offer.hotel_name, offer.country, _cached_evidence(offer)
        )
        now[0] += timedelta(hours=25)  # past the 24h TTL
        transport = FakeTransport([_place(place_id="fresh-place")])
        provider = GoogleRatingProvider(
            store=store, transport=transport, cache_ttl=timedelta(hours=24)
        )

        result = provider.verify(offer)

        assert transport.queries  # Google was queried again
        assert result is not None
        assert result.external_id == "fresh-place"


# --- 10/11. Cache keyed by hotel, not by offer --------------------------------------


def test_same_hotel_in_two_offers_triggers_one_request(
    offer: Offer, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    with Store(settings.database) as store:
        transport = FakeTransport([_place()])
        provider = GoogleRatingProvider(store=store, transport=transport)
        second = replace(offer, offer_id=offer.offer_id + "-other-date")

        result_a = provider.verify(offer)
        result_b = provider.verify(second)

        assert len(transport.queries) == 1
        assert result_a is not None and result_b is not None
        assert result_a.external_id == result_b.external_id == "place-1"


def test_two_different_hotels_get_separate_cache_entries(
    offer: Offer, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    with Store(settings.database) as store:
        other = replace(offer, hotel_name="Desert Demo", country="EG", destination="Hurghada")
        transport = FakeTransport(
            [_place(name="Sunny Demo", place_id="place-1", address="Main Rd, Crete, Greece")]
        )
        provider = GoogleRatingProvider(store=store, transport=transport)
        provider.verify(offer)

        transport.places = [
            _place(name="Desert Demo", place_id="place-2", address="Corniche, Hurghada, Egypt")
        ]
        provider.verify(other)

        assert len(transport.queries) == 2
        assert offer.hotel_name is not None and offer.country is not None
        assert other.hotel_name is not None and other.country is not None
        first_cached = store.get_cached_hotel_rating(
            "google", offer.hotel_name, offer.country, timedelta(hours=24)
        )
        second_cached = store.get_cached_hotel_rating(
            "google", other.hotel_name, other.country, timedelta(hours=24)
        )
        assert first_cached is not None and first_cached.external_id == "place-1"
        assert second_cached is not None and second_cached.external_id == "place-2"


# --- 12. Jungle Aqua Park vs. Water Valley: the live-test regression --------
#
# Live-confirmed 2026-09-28: querying Google for "Pickalbatros Jungle Aqua
# Park Resort Neverland Hurghada Egypt" returns the correct hotel as
# "Pickalbatros Jungle Aqua Park Resort - Neverland Hurghada" (Google pads
# the searched name with a trailing city -- see `_hotel_name_matches` in
# providers/external_rating.py) alongside an unrelated, similarly named
# sister property, "Pickalbatros Water Valley Resort - Neverland Hurghada".


def _neverland_offer(
    offer: Offer, hotel_name: str = "Pickalbatros Jungle Aqua Park Resort Neverland"
) -> Offer:
    return replace(offer, hotel_name=hotel_name, country="EG", destination="Hurghada")


def _jungle_aqua_park_place(place_id: str = "place-jungle") -> GooglePlace:
    return _place(
        name="Pickalbatros Jungle Aqua Park Resort - Neverland Hurghada",
        rating=4.7,
        reviews=26384,
        address="Hurghada, Red Sea Governorate, Egypt",
        place_id=place_id,
    )


def _water_valley_place(place_id: str = "place-water-valley") -> GooglePlace:
    return _place(
        name="Pickalbatros Water Valley Resort - Neverland Hurghada",
        rating=4.6,
        reviews=1428,
        address="Hurghada, Red Sea Governorate, Egypt",
        place_id=place_id,
    )


def test_correct_hotel_is_matched_despite_googles_trailing_city_suffix(
    offer: Offer, google_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    candidate = _neverland_offer(offer)
    transport = FakeTransport([_jungle_aqua_park_place()])
    provider = GoogleRatingProvider(transport=transport)

    result = verify_external(candidate, provider, google_settings.external_verification)

    evidence = result.hotel_ratings["google"]
    assert result.external_verification_statuses["google"] == "verified"
    assert evidence.rating == 4.7
    assert evidence.number_of_reviews == 26384


def test_similarly_named_sister_resort_alone_is_never_confused_for_a_match(
    offer: Offer, google_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    candidate = _neverland_offer(offer)
    transport = FakeTransport([_water_valley_place()])
    provider = GoogleRatingProvider(transport=transport)

    result = verify_external(candidate, provider, google_settings.external_verification)

    assert result.hotel_ratings == {}
    assert result.external_verification_statuses["google"] == "rejected_match"


def test_the_correct_hotel_is_still_picked_when_the_sister_resort_is_also_returned(
    offer: Offer, google_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two candidates come back, but only one actually matches the searched
    name -- the ambiguity rule only applies when several candidates *match*,
    not merely when several are *returned*."""
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    candidate = _neverland_offer(offer)
    transport = FakeTransport([_jungle_aqua_park_place(), _water_valley_place()])
    provider = GoogleRatingProvider(transport=transport)

    result = verify_external(candidate, provider, google_settings.external_verification)

    evidence = result.hotel_ratings["google"]
    assert result.external_verification_statuses["google"] == "verified"
    assert evidence.external_id == "place-jungle"
    assert evidence.rating == 4.7


def test_two_results_matching_the_same_name_are_rejected_not_guessed(
    offer: Offer, google_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    candidate = _neverland_offer(offer)
    transport = FakeTransport(
        [_jungle_aqua_park_place(place_id="place-a"), _jungle_aqua_park_place(place_id="place-b")]
    )
    provider = GoogleRatingProvider(transport=transport)

    result = verify_external(candidate, provider, google_settings.external_verification)

    assert result.hotel_ratings == {}
    assert result.external_verification_statuses["google"] == "rejected_match"


def test_an_accepted_alias_spelling_is_matched_against_googles_own_padding(
    offer: Offer, google_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different provider may return one of this hotel's other accepted
    watchlist aliases (see hotel_watchlist.json) instead of the full name --
    e.g. "Jungle Aqua Park - Neverland" rather than the canonical
    "Pickalbatros Jungle Aqua Park Resort Neverland". As long as Google's own
    response still carries every word of *that* alias, unbroken, the match
    still succeeds -- the same padding rule as for the full name."""
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "test-key")
    candidate = _neverland_offer(offer, hotel_name="Jungle Aqua Park - Neverland")
    transport = FakeTransport(
        [
            _place(
                name="Jungle Aqua Park - Neverland Hurghada",
                rating=4.7,
                reviews=26384,
                address="Hurghada, Red Sea Governorate, Egypt",
            )
        ]
    )
    provider = GoogleRatingProvider(transport=transport)

    result = verify_external(candidate, provider, google_settings.external_verification)

    assert result.external_verification_statuses["google"] == "verified"
    assert result.hotel_ratings["google"].rating == 4.7
