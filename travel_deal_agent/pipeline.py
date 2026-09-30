"""Filter and optionally enrich offers independently of polling schedules."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, timedelta

from . import watchlist
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


def _default_google_provider(settings: Settings, store: Store) -> GoogleRatingProvider:
    """Wire the real Google provider to this run's cache store and configured
    scale/TTL (see `config_types.ExternalSourceConfig`); a source absent from
    configuration falls back to the provider's own defaults."""
    policy = settings.external_verification.get("sources", {}).get("google")
    if policy is None:
        return GoogleRatingProvider(store=store)
    return GoogleRatingProvider(
        store=store,
        cache_ttl=timedelta(hours=policy["cache_ttl_hours"]),
        scale_min=policy["scale"]["min"],
        scale_max=policy["scale"]["max"],
    )


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
                else [_default_google_provider(settings, store), TripadvisorRatingProvider()]
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

    def filter_watchlist_batch(
        self, name: str, offers: list[Offer], already_accepted: list[Offer]
    ) -> list[Offer]:
        """Match this provider's raw batch against the hotel watchlist.

        Entirely independent of `filter_batch`'s standard eligibility: each
        watched hotel has its own price ceiling, stay length and airport
        rules (see `watchlist.py`), never `self.settings.filters["max_price"]`
        or the board/rating/star rules. Offers already accepted by the
        standard search are excluded here so the same offer is never
        notified twice for happening to satisfy both.
        """
        already_accepted_ids = {(o.provider, o.offer_id) for o in already_accepted}
        accept_incomplete = self.settings.filters.get("accept_incomplete_price_from", [])
        today = self.today()
        accepted = []
        for offer in offers:
            if offer.provider != name or (offer.provider, offer.offer_id) in already_accepted_ids:
                continue
            if watchlist.matches_any(
                offer, self.settings.hotel_watchlist, accept_incomplete, today
            ):
                accepted.append(offer)
        return accepted

    def finalize(
        self, offers: list[Offer], watchlist_offer_ids: frozenset[tuple[str, str]] = frozenset()
    ) -> list[Offer]:
        """Verify only top eligible candidates, then save and rank final results.

        `watchlist_offer_ids` marks which of `offers` were found via the hotel
        watchlist rather than the standard search (see
        `filter_watchlist_batch`); it only tags the stored alert `kind` (see
        `Store.observe`) so notification rendering can skip the HOT/GOOD/MATCH
        attractiveness category for a watched-hotel alert. It never changes
        ranking, deduplication or eligibility.
        """
        offers = [replace(o, hotel_ratings={}, external_verification_statuses={}) for o in offers]
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
            )
            kind_prefix = (
                "watchlist_" if (offer.provider, offer.offer_id) in watchlist_offer_ids else ""
            )
            for event in self.store.observe(offer, True, kind_prefix=kind_prefix):
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
