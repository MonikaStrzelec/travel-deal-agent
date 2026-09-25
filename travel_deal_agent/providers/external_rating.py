"""Independent hotel-rating contracts and conservative offline verification."""

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import replace

from ..boards import normalized_text
from ..config_types import ExternalConfig
from ..models import ExternalHotelRating, Offer

logger = logging.getLogger(__name__)


class ExternalHotelRatingProvider(ABC):
    source: str = "google"

    @abstractmethod
    def verify(self, offer: Offer) -> ExternalHotelRating | None:
        """Use name, ISO country and region to find a unique hotel; never guess."""


class GoogleRatingProvider(ExternalHotelRatingProvider):
    """Offline extension point. Enabling configuration performs no network access."""

    source = "google"

    def verify(self, offer: Offer) -> ExternalHotelRating | None:
        return None


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
                or normalized_text(result.matched_hotel_name) != normalized_text(offer.hotel_name)
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
    return replace(
        offer,
        hotel_ratings=results,
        external_verification_statuses=statuses,
        google_rating=None,
        external_rating_status="verified" if results else "external rating not verified",
    )
