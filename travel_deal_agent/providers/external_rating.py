"""Independent hotel-rating contracts and conservative offline verification."""

import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta

from ..boards import normalized_text
from ..config_types import ExternalConfig
from ..models import ExternalHotelRating, Offer
from ..storage import Store
from ..watchlist import normalize_hotel_name
from .google_places import (
    API_KEY_ENV,
    COUNTRY_NAMES_EN,
    FIELD_MASK,
    GooglePlace,
    GooglePlacesTransport,
    UrllibGooglePlacesTransport,
    country_from_address,
)

logger = logging.getLogger(__name__)


class ExternalHotelRatingProvider(ABC):
    source: str = "google"

    @abstractmethod
    def verify(self, offer: Offer) -> ExternalHotelRating | None:
        """Use name, ISO country and region to find a unique hotel; never guess."""


def _hotel_name_matches(hotel_name: str, candidate_name: str) -> bool:
    """A conservative "same hotel" check between our search name and a
    source's own returned name.

    Deliberately not fuzzy matching (see `watchlist.py`'s module docstring
    for the same philosophy applied to hotel names): every word of
    `hotel_name` (after the shared `normalize_hotel_name` normalization --
    diacritics, case and punctuation stripped) must appear, unbroken and in
    the same order, somewhere in `candidate_name`. Exact equality is the
    common case, but a source may also legitimately pad the name with extra
    words on either side -- most often Google's Text Search echoing back the
    hotel's own city/region (e.g. "...Resort - Neverland" becomes "...Resort
    - Neverland Hurghada"). No word of `hotel_name` itself may be dropped,
    reordered or substituted, so a different, similarly-named property (e.g.
    a sibling sub-resort) is never accepted.
    """
    target = normalize_hotel_name(hotel_name).split()
    candidate = normalize_hotel_name(candidate_name).split()
    if not target:
        return False
    span = len(target)
    return any(candidate[i : i + span] == target for i in range(len(candidate) - span + 1))


def _select_google_match(
    hotel_name: str, places: Sequence[GooglePlace]
) -> tuple[GooglePlace, float, bool] | None:
    """Pick a Text Search candidate, or report Google's own results as ambiguous.

    See `_hotel_name_matches` for the matching rule. Everything else -- no
    candidates, no matching candidate, or more than one candidate matching
    the searched name -- is either reported as `ambiguous` or left for
    `verify_external`'s own name/country/location checks to reject; this
    function never discards a candidate outright, so every rejection reason
    stays visible in `Offer.external_verification_statuses`.
    """
    if not places:
        return None
    named_matches = [
        place for place in places if _hotel_name_matches(hotel_name, place.display_name)
    ]
    if len(named_matches) > 1:
        # More than one of Google's own results matches this hotel's name;
        # picking the first would be a guess, not a match.
        return named_matches[0], 0.5, True
    if named_matches:
        return named_matches[0], 0.95, False
    # No matching name at all -- report the top candidate as-is (with low
    # confidence) so verify_external's own name check rejects it rather than
    # this function silently discarding a real Google response.
    return places[0], 0.4, False


class GoogleRatingProvider(ExternalHotelRatingProvider):
    """Google Places API (New) Text Search, cached and offline-safe.

    Returns `None` -- never raises -- when `GOOGLE_PLACES_API_KEY` is unset,
    or when the offer lacks a hotel name or country to search on; every other
    failure (HTTP error, timeout, malformed response) propagates so
    `verify_external` logs and isolates it like any other source failure
    (never disguised as a "no match").

    `store`, when given, caches a confident, unambiguous match for
    `cache_ttl` (see `storage.Store.get_cached_hotel_rating`), keyed on the
    normalized hotel name and country -- so the same hotel appearing in
    several offers within one scheduler cycle, or across cycles inside the
    TTL, triggers at most one Google request. `scale_min`/`scale_max` mirror
    `external_verification.sources.google.scale` in configuration (Google's
    own native scale is 1-5, but this is never hardcoded here -- see
    AGENTS.md "preserve source-specific native rating scales").
    """

    source = "google"

    def __init__(
        self,
        store: Store | None = None,
        transport: GooglePlacesTransport | None = None,
        cache_ttl: timedelta = timedelta(hours=24),
        scale_min: float = 1,
        scale_max: float = 5,
        timeout: float = 10.0,
    ) -> None:
        self.store = store
        self.transport = transport or UrllibGooglePlacesTransport()
        self.cache_ttl = cache_ttl
        self.scale_min = scale_min
        self.scale_max = scale_max
        self.timeout = timeout

    def verify(self, offer: Offer) -> ExternalHotelRating | None:
        if not offer.hotel_name or not offer.country:
            return None
        if self.store is not None:
            cached = self.store.get_cached_hotel_rating(
                "google", offer.hotel_name, offer.country, self.cache_ttl
            )
            if cached is not None:
                return cached
        api_key = os.environ.get(API_KEY_ENV, "").strip()
        if not api_key:
            logger.debug("%s not set; skipping Google verification", API_KEY_ENV)
            return None
        query = ", ".join(
            part
            for part in (
                offer.hotel_name,
                offer.destination,
                COUNTRY_NAMES_EN.get(offer.country, offer.country),
            )
            if part
        )
        places = self.transport.search_text(query, api_key, FIELD_MASK, self.timeout)
        selected = _select_google_match(offer.hotel_name, places)
        if selected is None:
            return None
        place, confidence, ambiguous = selected
        if place.rating is None:
            return None
        matched_country = country_from_address(place.formatted_address)
        result = ExternalHotelRating(
            source="google",
            rating=place.rating,
            scale_min=self.scale_min,
            scale_max=self.scale_max,
            number_of_reviews=place.user_rating_count,
            matched_hotel_name=place.display_name,
            country=matched_country,
            confidence=confidence,
            location=offer.destination,
            external_id=place.place_id,
            ambiguous=ambiguous,
        )
        if self.store is not None and not ambiguous and matched_country == offer.country:
            self.store.save_cached_hotel_rating("google", offer.hotel_name, offer.country, result)
        return result


class TripadvisorRatingProvider(ExternalHotelRatingProvider):
    """Offline extension point; no scraping or API access is implemented."""

    source = "tripadvisor"

    def verify(self, offer: Offer) -> ExternalHotelRating | None:
        return None


def verify_external(
    offer: Offer,
    providers: ExternalHotelRatingProvider | Sequence[ExternalHotelRatingProvider] | None,
    config: ExternalConfig,
) -> Offer:
    """Isolate each source failure; reject weak, ambiguous or conflicting evidence."""
    adapters = (
        []
        if providers is None
        else (
            [providers] if isinstance(providers, ExternalHotelRatingProvider) else list(providers)
        )
    )
    registered = {p.source: p for p in adapters}
    if len(registered) != len(adapters):
        raise ValueError("External provider sources must be unique")
    results: dict[str, ExternalHotelRating] = {}
    statuses: dict[str, str] = {}
    for source, policy in config.get("sources", {}).items():
        statuses[source] = "not_checked"
        provider = registered.get(source)
        if not config["enabled"] or not policy["enabled"] or provider is None:
            continue
        # Name-only matching is never sufficient. Require country evidence too.
        if not offer.hotel_name or not offer.country:
            statuses[source] = "insufficient_identity"
            continue
        try:
            result = provider.verify(offer)
            if result is None:
                statuses[source] = "no_match"
                continue
            if (
                result.source != source
                or result.ambiguous
                or result.confidence < policy["min_confidence"]
                or result.country != offer.country
                or not _hotel_name_matches(offer.hotel_name, result.matched_hotel_name)
                or (
                    offer.destination
                    and normalized_text(result.location or "") != normalized_text(offer.destination)
                )
                or result.scale_min != policy["scale"]["min"]
                or result.scale_max != policy["scale"]["max"]
            ):
                statuses[source] = "rejected_match"
                continue
            results[source] = result
            statuses[source] = "verified"
        except Exception:
            logger.exception("External source %s failed for %s", source, offer.offer_id)
            statuses[source] = "error"
    return replace(offer, hotel_ratings=results, external_verification_statuses=statuses)
