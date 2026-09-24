"""Filter and optionally enrich offers independently of polling schedules."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date

from .config import Settings
from .filtering import matches
from .models import Offer
from .providers.external_rating import (
    ExternalHotelRatingProvider,
    GoogleRatingProvider,
    TripadvisorRatingProvider,
    verify_external,
)
from .ranking import deduplicate, rank_offers, score
from .storage import Store

logger = logging.getLogger(__name__)


class OfferPipeline:
    """Apply eligibility, candidate selection, enrichment and persistence in order."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        external_provider: ExternalHotelRatingProvider | None = None,
        today: Callable[[], date] = date.today,
        external_providers: Sequence[ExternalHotelRatingProvider] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        if external_provider is not None and external_providers is not None:
            raise ValueError("Use either external_provider or external_providers")
        self.external_providers = (
            list(external_providers)
            if external_providers is not None
            else (
                [external_provider]
                if external_provider is not None
                else [GoogleRatingProvider(), TripadvisorRatingProvider()]
            )
        )
        if len({p.source for p in self.external_providers}) != len(self.external_providers):
            raise ValueError("External provider sources must be unique")
        self.today = today

    def filter_batch(self, name: str, offers: list[Offer]) -> list[Offer]:
        """Isolate malformed source records while allowing storage errors to propagate."""
        accepted = []
        for offer in offers:
            if offer.provider != name:
                logger.error("Rejected offer %s: provider does not match %s", offer.offer_id, name)
                continue
            if matches(offer, self.settings.filters, today=self.today()):
                accepted.append(offer)
            else:
                self.store.observe(offer, False)
        return accepted

    def finalize(self, offers: list[Offer]) -> list[Offer]:
        """Verify only top eligible candidates, then save and rank final results."""
        offers = [
            replace(
                o,
                hotel_ratings={},
                external_verification_statuses={},
                google_rating=None,
                external_rating_status="external rating not verified",
            )
            for o in offers
        ]
        preliminary = self._rank(deduplicate(offers))
        limit = self.settings.external_verification["max_candidates"]
        verified = {
            (o.provider, o.offer_id): verify_external(
                o, self.external_providers, self.settings.external_verification
            )
            for o in preliminary[:limit]
        }
        enriched = []
        for offer in offers:
            offer = verified.get((offer.provider, offer.offer_id), offer)
            rule = self.settings.filters["provider_ratings"].get(offer.provider)
            scale = rule["scale"] if rule else None
            offer = replace(
                offer,
                final_score=score(offer, self.settings.ranking, self.settings.filters["max_price"]),
                provider_rating_max=scale["max"] if scale else None,
                google_rating_max=self.settings.external_verification["scale"]["max"],
            )
            for event in self.store.observe(offer, True):
                logger.info(
                    "%s: %s/%s price=%s",
                    event,
                    offer.provider,
                    offer.offer_id,
                    offer.price_per_person,
                )
            enriched.append(offer)
        return self._rank(deduplicate(enriched))

    def _rank(self, offers: list[Offer]) -> list[Offer]:
        return rank_offers(offers, self.settings.ranking, self.settings.filters["max_price"])
