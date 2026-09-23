"""Rainbow source adapter: bounded collection, no ranking, persistence or notifications."""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from ..config_types import FilterConfig, ProviderConfig
from ..models import Offer, utc_now
from .base import Provider
from .rainbow_browser import Listing, open_listing
from .rainbow_config import Limits, SearchPlan
from .rainbow_data import parse_card
from .rainbow_details import parse_selected_variant
from .rainbow_enrichment import enrich_selected, potential_candidate
from .rainbow_errors import RainbowError, RainbowStructureError
from .rainbow_nuxt import RainbowDetailError

logger = logging.getLogger(__name__)
ListingFactory = Callable[[SearchPlan, Limits], AbstractContextManager[Listing]]


class RainbowProvider(Provider):
    name = "rainbow"

    def __init__(
        self,
        configuration: ProviderConfig,
        filters: FilterConfig,
        listing_factory: ListingFactory = open_listing,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.plan = SearchPlan.from_filters(filters)
        self.filters = filters
        self.limits = Limits.from_config(configuration)
        self.listing_factory, self.clock = listing_factory, clock

    def fetch(self) -> list[Offer]:
        try:
            with self.listing_factory(self.plan, self.limits) as listing:
                return self._enrich(listing, self._collect(listing))
        except RainbowError:
            logger.exception("Rainbow source failure")
            raise
        except Exception as exc:
            logger.exception("Rainbow technical failure")
            raise RainbowError("Rainbow technical failure") from exc

    def _collect(self, listing: Listing) -> list[Offer]:
        offers: dict[str, Offer] = {}
        analyzed = scrolls = index = 0
        previous_price = Decimal(0)
        observed = self.clock()
        while analyzed < self.limits.max_analyzed_cards and len(offers) < self.limits.max_offers:
            count = listing.count()
            if index >= count:
                if not count or scrolls >= self.limits.max_scrolls:
                    break
                scrolls += 1
                if not listing.advance():
                    break
                continue
            analyzed += 1
            offer = parse_card(listing.card_html(index), observed)
            index += 1
            assert offer.price_per_person is not None
            if offer.price_per_person < previous_price:
                raise RainbowStructureError("Rainbow cards are not in ascending price order")
            previous_price = offer.price_per_person
            if previous_price > self.plan.max_price:
                break
            offers.setdefault(offer.offer_id, offer)
        logger.info(
            "Rainbow scan complete: %s candidates, %s analyzed cards, %s scrolls",
            len(offers),
            analyzed,
            scrolls,
        )
        return list(offers.values())

    def _enrich(self, listing: Listing, offers: list[Offer]) -> list[Offer]:
        attempts = confirmed = 0
        result: list[Offer] = []
        for offer in offers:
            if attempts >= self.limits.max_detail_requests or not potential_candidate(
                offer,
                self.filters,
                offer.found_at.date(),
            ):
                result.append(offer)
                continue
            try:
                evidence = listing.evidence(offer)
                if evidence is None:
                    offer = replace(
                        offer, price_verification_reason="No matching structured listing evidence"
                    )
                elif potential_candidate(offer, self.filters, offer.found_at.date(), evidence):
                    attempts += 1
                    variant = parse_selected_variant(
                        listing.detail_html(evidence.url),
                        expected_product_key=evidence.product_key,
                        expected_opaque_key=evidence.opaque_key,
                        expected_birth_dates=evidence.birth_dates,
                    )
                    offer = enrich_selected(offer, evidence, variant)
                    confirmed += 1
            except RainbowDetailError as exc:
                logger.warning("Rainbow variant remains unverified: %s", exc)
                offer = replace(offer, price_verification_reason=str(exc))
            result.append(offer)
        logger.info(
            "Rainbow details: %s attempted, %s configurations confirmed", attempts, confirmed
        )
        return result
