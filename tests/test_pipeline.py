"""End-to-end proof of the eligible/price-completeness contract described in
`Store.observe`'s docstring: `filtering.matches()` is the single place that
decides whether an offer's price is acceptable to alert on (including the
`accept_incomplete_price_from` per-provider whitelist), and everything
downstream -- storage, and by extension notification rendering -- trusts that
decision instead of re-deriving it.
"""

from dataclasses import replace

from travel_deal_agent.config import Settings
from travel_deal_agent.filtering import matches
from travel_deal_agent.models import Offer
from travel_deal_agent.pipeline import OfferPipeline
from travel_deal_agent.storage import Store


def test_filter_batch_passes_eligible_exactly_when_matches_says_so(
    offer: Offer, settings: Settings, store: Store
) -> None:
    # Arrange: one offer that matches (mock, complete price) and one that does
    # not (mock, incomplete price and not whitelisted).
    matching = offer
    non_matching = replace(offer, offer_id="non-matching", price_is_complete=False)
    pipeline = OfferPipeline(settings, store, external_providers=[])
    accepted = pipeline.filter_batch("mock", [matching, non_matching])
    # Assert: filter_batch's return value is exactly the set matches() accepts.
    assert accepted == [o for o in (matching, non_matching) if matches(o, settings.filters)]
    assert accepted == [matching]


def test_whitelisted_wakacje_offer_with_incomplete_price_produces_an_alert(
    offer: Offer, settings: Settings, store: Store
) -> None:
    # Arrange: config.json whitelists wakacje.pl; this offer otherwise matches
    # every business criterion.
    assert "wakacje.pl" in settings.filters.get("accept_incomplete_price_from", [])
    candidate = replace(
        offer, provider="wakacje.pl", offer_id="wakacje-e2e", price_is_complete=False, rating=8.4
    )
    pipeline = OfferPipeline(settings, store, external_providers=[])
    accepted = pipeline.filter_batch(candidate.provider, [candidate])
    pipeline.finalize(accepted)
    # Assert: the normal pipeline -- not a direct Store.observe call -- created a
    # real, pending alert for an offer whose price was never confirmed complete.
    assert len(accepted) == 1
    pending = store.pending()
    assert len(pending) == 1
    assert "wakacje-e2e" in pending[0]["payload"]


def test_non_whitelisted_provider_with_incomplete_price_produces_no_alert(
    offer: Offer, settings: Settings, store: Store
) -> None:
    # Arrange: "mock" is not in accept_incomplete_price_from -- an otherwise
    # fully matching offer with an incomplete price must not alert through the
    # normal pipeline, exactly as before this change.
    assert "mock" not in settings.filters.get("accept_incomplete_price_from", [])
    candidate = replace(offer, offer_id="mock-e2e", price_is_complete=False)
    pipeline = OfferPipeline(settings, store, external_providers=[])
    accepted = pipeline.filter_batch(candidate.provider, [candidate])
    pipeline.finalize(accepted)
    assert accepted == []
    assert store.pending() == []
