"""Offline progressive collection and shared eligibility regression tests."""

from dataclasses import replace
from datetime import date

import pytest

from experiments.itaka_playwright.listing import Card, collect_candidates, listing_offer, qualifies
from experiments.itaka_playwright.settings import PocLimits, load_business_filters
from travel_deal_agent.filtering import matches, matches_criteria


class FakeCards:
    def __init__(self, batches: list[list[str]]) -> None:
        self.batches = batches
        self.position = 0
        self.reads: list[str] = []
        self.scrolls = 0

    def keys(self) -> list[str]:
        return self.batches[min(self.position, len(self.batches) - 1)]

    def read(self, key: str) -> Card:
        self.reads.append(key)
        return {"url": key}

    def advance(self) -> None:
        self.position += 1
        self.scrolls += 1


def test_rejections_scroll_duplicates_are_not_reprocessed_and_target_stops_immediately() -> None:
    # Arrange
    source = FakeCards([["RO"], ["RO", "HB", "HB", "AI", "extra"]])
    # Act
    result = collect_candidates(source, PocLimits(max_offers=2), lambda c: c["url"] != "RO")
    # Assert
    assert [c["url"] for c in result.candidates] == ["HB", "AI"]
    assert source.reads == ["RO", "HB", "AI"]
    assert source.scrolls == 1
    assert result.stop_reason == "candidate_limit"


@pytest.mark.parametrize(
    "reason,cap,scrolls,idle",
    [
        ("analysis_limit", 2, 20, 3),
        ("scroll_limit", 50, 1, 3),
        ("no_new_cards", 50, 20, 2),
    ],
)
def test_collection_is_bounded(reason: str, cap: int, scrolls: int, idle: int) -> None:
    # Arrange
    source = FakeCards([["a", "b", "c"]])
    limits = PocLimits(
        max_offers=10, max_analyzed_cards=cap, max_scrolls=scrolls, max_idle_scrolls=idle
    )
    # Act
    result = collect_candidates(source, limits, lambda _: False)
    # Assert
    assert result.stop_reason == reason
    assert result.analyzed <= cap
    assert result.scrolls <= scrolls
    assert len(source.reads) == len(set(source.reads))


def card(board: str = "2 posiłki") -> Card:
    return {
        "url": "/wczasy/malta/example,ABC/?id=A&adults%5B0%5D=2",
        "hotel_name": "Example",
        "destination": "Malta",
        "star_icons": "3",
        "price_text": "1 200 zł /os.",
        "rating_text": "5.3\n/6",
        "text": f"Example\n5.12 - 11.12.2026 (7 dni)\nKatowice 14:00\n{board}\n"
        "36 opinii\n+20 zł (TFG i TFP)",
    }


def test_candidates_reuse_rules_without_claiming_confirmed_price() -> None:
    # Arrange
    filters = load_business_filters()
    today = date(2026, 9, 20)
    raw = card()
    # Act
    offer = listing_offer(raw)
    # Assert
    assert qualifies(raw, filters, today)
    assert matches_criteria(offer, filters, today)
    assert not matches(offer, filters, today)
    assert matches(replace(offer, price_is_complete=True), filters, today)
    assert str(offer.price_per_person) == "1220"
    assert offer.number_of_reviews == 36
    assert not qualifies(card("Bez wyżywienia"), filters, today)
    assert not qualifies(card("Śniadania"), filters, today)
    assert not qualifies({**raw, "rating_text": None}, filters, today)
    assert not qualifies({**raw, "url": "/wczasy/turcja/example/?adults%5B0%5D=2"}, filters, today)
    assert not qualifies({**raw, "price_text": "1 490 zł /os."}, filters, today)
    assert not qualifies(raw, filters, date(2027, 1, 1))


def test_invalid_collection_limits_are_rejected() -> None:
    with pytest.raises(ValueError):
        PocLimits(max_offers=10, max_analyzed_cards=0)
