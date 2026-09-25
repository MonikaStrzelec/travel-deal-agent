"""Offline Rainbow parsing, collection and shared-pipeline integration."""

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from travel_deal_agent.config import Settings
from travel_deal_agent.config_types import FilterConfig, ProviderConfig
from travel_deal_agent.filtering import matches
from travel_deal_agent.models import Offer
from travel_deal_agent.notifications import ConsoleNotifier
from travel_deal_agent.pipeline import OfferPipeline
from travel_deal_agent.providers.rainbow import RainbowProvider
from travel_deal_agent.providers.rainbow_browser import Listing
from travel_deal_agent.providers.rainbow_config import STAR_CODES, Limits, SearchPlan, star_options
from travel_deal_agent.providers.rainbow_data import parse_card, parse_price
from travel_deal_agent.providers.rainbow_errors import (
    RainbowBlocked,
    RainbowError,
    RainbowStructureError,
    RainbowTimeout,
)
from travel_deal_agent.providers.rainbow_listing_data import ListingEvidence
from travel_deal_agent.providers.registry import build_providers
from travel_deal_agent.scheduler import Scheduler
from travel_deal_agent.storage import Store

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)
HTML = (Path(__file__).parent / "fixtures/rainbow/card.html").read_text(encoding="utf-8")


class FakeListing:
    def __init__(self, cards: list[str], more: list[str] | None = None) -> None:
        self.cards = cards
        self.more = more or []
        self.reads: list[int] = []
        self.scrolls = 0
        self.closed = False
        self.error: Exception | None = None

    def count(self) -> int:
        if self.error:
            raise self.error
        return len(self.cards)

    def card_html(self, index: int) -> str:
        self.reads.append(index)
        return self.cards[index]

    def advance(self) -> bool:
        self.scrolls += 1
        self.cards.extend(self.more)
        changed = bool(self.more)
        self.more = []
        return changed

    def evidence(self, offer: Offer) -> ListingEvidence | None:
        return None

    def detail_html(self, url: str) -> str:
        raise AssertionError("No detail request without structured evidence")

    @contextmanager
    def open(self, plan: SearchPlan, limits: Limits) -> Iterator[Listing]:
        try:
            yield self
        finally:
            self.closed = True


def provider(settings: Settings, listing: FakeListing, **limits: int) -> RainbowProvider:
    configuration: ProviderConfig = {
        "enabled": True,
        "interval_seconds": 3600,
        "max_offers": limits.get("max_offers", 10),
        "max_analyzed_cards": limits.get("max_analyzed_cards", 15),
        "max_scrolls": limits.get("max_scrolls", 0),
    }
    return RainbowProvider(configuration, settings.filters, listing.open, lambda: NOW)


def card(index: int = 0, price: int = 1400) -> str:
    return HTML.replace("1 551", str(price)).replace("gardenia-hotel", f"hotel-{index}")


@pytest.mark.parametrize("minimum,codes", [(3, ["6", "8", "10"]), (4, ["8", "10"]), (5, ["10"])])
def test_stars_are_separate_source_options(minimum: int, codes: list[str]) -> None:
    assert [STAR_CODES[s] for s in star_options(minimum)] == codes


@pytest.mark.parametrize("minimum", [2, 3.5, 6])
def test_unobserved_star_options_fail_at_configuration(minimum: float) -> None:
    with pytest.raises(ValueError, match="minimum stars"):
        star_options(minimum)


@pytest.mark.parametrize(
    "text,expected",
    [("1 500zł/os.", "1500"), ("1\u00a0499,99 zł/os.", "1499.99"), ("999.50zł/os.", "999.50")],
)
def test_price_is_decimal_per_person(text: str, expected: str) -> None:
    assert parse_price(text) == Decimal(expected)


@pytest.mark.parametrize(
    "text", ["1500 EUR", "3000 zł razem", "od 1500zł/os.", "-1zł/os.", "0zł/os.", "1,234,56zł/os."]
)
def test_price_ambiguity_is_not_silently_normalized(text: str) -> None:
    with pytest.raises(RainbowStructureError):
        parse_price(text)


def test_recorded_card_normalizes_without_inventing_a_variant() -> None:
    # Arrange / act.
    result = parse_card(HTML, NOW)

    # Assert.
    assert result.provider == "rainbow"
    assert result.hotel_name == "Gardenia Hotel"
    assert result.country == "TR"
    assert result.destination == "Wypoczynek • Turcja: Riwiera Turecka"
    assert result.price_per_person == Decimal("1551")
    assert result.total_price == Decimal("3102")
    assert result.currency == "PLN" and result.number_of_people == 2
    assert result.hotel_stars == 4
    assert (result.rating, result.provider_rating_max, result.number_of_reviews) == (5.3, 6, 44)
    assert result.departure_date == date(2026, 12, 5)
    assert result.number_of_days == 8 and result.return_date is None
    assert result.departure_airport is None and result.board_type is None
    assert "(+1)" in (result.price_notes or "")
    assert not result.price_is_complete and not result.variant_verified
    assert result.variant_identity is None
    assert result.url == "https://r.pl/turcja-riwiera-wczasy/gardenia-hotel"
    assert result.found_at == result.last_seen == NOW


@pytest.mark.parametrize(
    "label,board", [("2 posiłki", "HB"), ("3 posiłki", "FB"), ("All inclusive", "AI")]
)
def test_unambiguous_meals_and_airport(label: str, board: str) -> None:
    html = HTML.replace(" <span>(+1)</span>", "").replace("2 posiłki", label)

    result = parse_card(html, NOW)

    assert result.board_type == board
    assert result.departure_airport == "KTW"


def test_optional_fields_can_be_absent() -> None:
    # Arrange: a separately authored minimal card, not generated by the parser.
    html = '<a href="/trip/hotel"><div data-test-id="r-bloczek:szukaj:0">'
    html += '<h3 data-test-id="r-typography:szukaj:tytul-0">Hotel</h3>'
    html += '<span data-test-id="r-typography:szukaj:cena-aktualna-0">900zł/os.</span></div></a>'

    result = parse_card(html, NOW)

    assert result.country is None and result.rating is None and result.number_of_reviews is None
    assert (
        result.departure_date is None and result.return_date is None and result.hotel_stars is None
    )
    assert result.board_type is result.departure_airport is None


@pytest.mark.parametrize(
    "old,new",
    [
        ('data-rating="4"', 'data-rating="3"'),
        ("5.3/6", "7.1/6"),
        ("(44 opinie)", "(45 opinie)"),
        ("05.12.2026", "32.12.2026"),
        ("8 dni / 7 noclegów", "0 dni / 7 noclegów"),
        ('href="/turcja', 'href="https://unrelated.invalid/turcja'),
        ("cena-aktualna-0", "changed-price-selector"),
        # A duplicated field is ambiguous, never resolved by picking one.
        ("</h3>", '</h3><h3 data-test-id="r-typography:szukaj:tytul-2">Other hotel</h3>'),
    ],
)
def test_malformed_present_fields_raise_structure_error(old: str, new: str) -> None:
    with pytest.raises(RainbowStructureError):
        parse_card(HTML.replace(old, new), NOW)


def test_listing_identity_survives_price_and_rating_changes_but_not_trip_changes() -> None:
    original = parse_card(HTML, NOW)

    assert parse_card(HTML.replace("1 551", "1 400"), NOW).offer_id == original.offer_id
    assert parse_card(HTML.replace("05.12.2026", "12.12.2026"), NOW).offer_id != original.offer_id
    assert parse_card(HTML.replace("Katowice", "Łódź"), NOW).offer_id != original.offer_id


def test_provider_stops_at_offer_limit(settings: Settings) -> None:
    listing = FakeListing([card(i) for i in range(20)])

    result = provider(settings, listing).fetch()

    assert len(result) == 10 and listing.reads == list(range(10))
    assert listing.scrolls == 0 and listing.closed


def test_duplicates_consume_card_budget(settings: Settings) -> None:
    listing = FakeListing([card()] * 30)

    result = provider(settings, listing).fetch()

    assert len(result) == 1 and listing.reads == list(range(15))


def test_empty_scan_is_success_and_does_not_scroll(settings: Settings) -> None:
    listing = FakeListing([])

    assert provider(settings, listing).fetch() == []
    assert listing.reads == [] and listing.scrolls == 0 and listing.closed


def test_over_budget_stops_sorted_collection(settings: Settings) -> None:
    listing = FakeListing([card(0, 1500), card(1, 1501), card(2, 1600)])

    result = provider(settings, listing).fetch()

    assert len(result) == 1 and listing.reads == [0, 1]


def test_scroll_budget_is_independent(settings: Settings) -> None:
    listing = FakeListing([card(0)], [card(1)])

    result = provider(settings, listing, max_scrolls=1).fetch()

    assert len(result) == 2 and listing.scrolls == 1


@pytest.mark.parametrize(
    "failure", [RainbowTimeout, RainbowBlocked, RainbowStructureError, RainbowError]
)
def test_errors_remain_distinguishable_and_close_session(
    settings: Settings, failure: type[RainbowError]
) -> None:
    listing = FakeListing([])
    listing.error = failure("Source failure")

    with pytest.raises(failure):
        provider(settings, listing).fetch()
    assert listing.closed


def test_shared_configuration_and_registry(settings: Settings) -> None:
    # Arrange.
    config: ProviderConfig = {"enabled": True, "interval_seconds": 100}
    filters = deepcopy(settings.filters)
    filters["min_stars"] = 4
    filters["max_price"] = "1234"
    filters["airports"] = ["LCJ", "WAW"]
    filters["allowed_boards"] = ["AI"]

    # Act.
    sources = build_providers({"rainbow": config}, filters=filters)

    # Assert: construction performs no browser access.
    assert isinstance(sources[0], RainbowProvider)
    assert sources[0].plan.stars == (4, 5)
    assert sources[0].plan.max_price == Decimal("1234")
    assert sources[0].plan.boards == ("AI",)
    assert sources[0].plan.airports == ("LCJ", "WAW")
    assert sources[0].plan.rating_floor == 5


def test_configured_defaults_are_production_not_diagnostic(settings: Settings) -> None:
    plan = SearchPlan.from_filters(settings.filters)

    assert plan.max_price == 1500 and (plan.min_days, plan.max_days) == (7, 9)
    assert plan.rating_floor == 5 and set(plan.boards) == {"HB", "FB", "AI"}
    assert not settings.providers["rainbow"]["enabled"]


def test_search_plan_supports_unrestricted_duration(settings: Settings) -> None:
    # Arrange: no stay-length restriction is the current business rule (config.json).
    filters = deepcopy(settings.filters)
    filters["min_nights"] = None
    filters["max_nights"] = None

    plan = SearchPlan.from_filters(filters)

    # Assert: Rainbow's own duration radio is left untouched (its default query
    # state, "dlugoscPobytu=*-*", already means every length).
    assert plan.min_days is None and plan.max_days is None


def test_search_plan_rejects_partial_duration_bounds(settings: Settings) -> None:
    # Arrange: one bound set without the other is not an observed Rainbow preset.
    filters = deepcopy(settings.filters)
    filters["min_nights"] = 6
    filters["max_nights"] = None

    with pytest.raises(ValueError, match="both min_nights and max_nights"):
        SearchPlan.from_filters(filters)


def test_search_plan_uses_only_confirmed_meal_options(settings: Settings) -> None:
    # Arrange: "ZO" is a shared board (added for ITAKA) that Rainbow has no
    # confirmed meal checkbox for -- narrow to what Rainbow can actually select
    # rather than failing configuration for every other provider's sake.
    filters = deepcopy(settings.filters)
    filters["allowed_boards"] = ["HB", "FB", "AI", "ZO"]

    plan = SearchPlan.from_filters(filters)

    assert set(plan.boards) == {"HB", "FB", "AI"}


def test_search_plan_rejects_meal_options_with_no_rainbow_support(settings: Settings) -> None:
    filters = deepcopy(settings.filters)
    filters["allowed_boards"] = ["UAI"]

    with pytest.raises(ValueError, match="Unsupported Rainbow meal option"):
        SearchPlan.from_filters(filters)


def test_missing_review_count_is_optional() -> None:
    html = HTML.replace(", 44 opinie", "").replace("<span>(44 opinie)</span>", "")

    result = parse_card(html, NOW)

    assert result.rating == 5.3 and result.number_of_reviews is None


def test_no_progress_scroll_stops_without_retry(settings: Settings) -> None:
    listing = FakeListing([card()])

    result = provider(settings, listing, max_scrolls=4).fetch()

    assert len(result) == 1 and listing.scrolls == 1


def test_descending_cards_are_not_silently_accepted(settings: Settings) -> None:
    listing = FakeListing([card(0, 1400), card(1, 1300)])

    with pytest.raises(RainbowStructureError, match="ascending"):
        provider(settings, listing).fetch()


def test_scan_deadline_after_progress_keeps_already_collected_offers(settings: Settings) -> None:
    # Arrange: the deadline strikes on the second card read, after the first
    # one already contributed a valid result.
    class FlakyListing(FakeListing):
        def card_html(self, index: int) -> str:
            if index >= 1:
                raise RainbowTimeout("Rainbow scan deadline exceeded")
            return super().card_html(index)

    listing = FlakyListing([card(0), card(1)])

    # Act.
    result = provider(settings, listing).fetch()

    # Assert: the offer read before the deadline is kept, not discarded.
    assert len(result) == 1 and listing.closed


@pytest.mark.parametrize(
    "max_offers,max_cards,scrolls", [(0, 15, 0), (10, 0, 0), (10, 5, 0), (10, 15, -1)]
)
def test_invalid_collection_limits_fail_early(
    max_offers: int, max_cards: int, scrolls: int
) -> None:
    with pytest.raises(ValueError):
        Limits.from_config(
            {
                "enabled": True,
                "interval_seconds": 10,
                "max_offers": max_offers,
                "max_analyzed_cards": max_cards,
                "max_scrolls": scrolls,
            }
        )


@pytest.mark.parametrize(
    "airport,expected",
    [("Łódź", "LCJ"), ("Warszawa Chopin", "WAW"), ("Warszawa Modlin", "WMI"), ("Wrocław", "WRO")],
)
def test_all_configured_airport_labels_are_normalized(airport: str, expected: str) -> None:
    html = HTML.replace("Katowice <span>(+1)</span>", airport)

    assert parse_card(html, NOW).departure_airport == expected


def test_shared_filters_reject_site_policy_violations(settings: Settings) -> None:
    # Arrange: simulate future complete variant evidence to exercise shared rules independently.
    base = parse_card(card().replace(" <span>(+1)</span>", ""), NOW)
    complete = replace(base, price_is_complete=True, return_date=date(2026, 12, 12))
    assert matches(complete, settings.filters, NOW.date())

    # Act / assert: UI filtering is never the acceptance authority.
    for bad in (
        replace(complete, rating=4.9),
        replace(complete, hotel_stars=2),
        replace(complete, return_date=date(2026, 12, 15)),
        replace(complete, departure_airport="KRK"),
        replace(complete, price_per_person=Decimal("1500.01")),
        replace(complete, board_type="BB"),
    ):
        assert not matches(bad, settings.filters, NOW.date())


def test_enrichment_deadline_keeps_offers_unenriched_instead_of_discarding_them(
    settings: Settings,
) -> None:
    # Arrange: the shortlist reaches detail, but the deadline strikes during
    # the evidence lookup, before any detail request is made.
    class TimingOutListing(FakeListing):
        def evidence(self, offer: Offer) -> ListingEvidence | None:
            raise RainbowTimeout("Rainbow scan deadline exceeded")

    listing = TimingOutListing([card()])

    # Act.
    result = provider(settings, listing).fetch()

    # Assert: the raw, unenriched offer survives instead of the whole fetch failing.
    assert len(result) == 1 and not result[0].variant_verified


def test_pipeline_persists_unverified_quotes_without_alerts(
    settings: Settings, store: Store
) -> None:
    # Arrange.
    pipeline = OfferPipeline(settings, store, today=lambda: NOW.date())
    first = parse_card(card(), NOW)
    cheaper = parse_card(card(price=1300), NOW)

    # Act.
    assert pipeline.filter_batch("rainbow", [first]) == []
    assert pipeline.filter_batch("rainbow", [cheaper]) == []

    # Assert: existing SQLite history and completeness gates remain in force.
    assert len(store.price_history("rainbow", first.offer_id)) == 2
    saved = store.get_offer("rainbow", first.offer_id)
    assert saved is not None
    assert saved.price_per_person == Decimal("1300")
    assert saved.return_date is None and not saved.variant_verified
    assert store.pending() == []
    assert not matches(first, settings.filters, NOW.date())


def test_empty_scan_resets_backoff_and_schedules_normally(settings: Settings, store: Store) -> None:
    cfg: ProviderConfig = {"enabled": True, "interval_seconds": 100}
    selected = replace(settings, providers={"rainbow": cfg})
    listing = FakeListing([])
    store.schedule("rainbow", 0, 3)
    scheduler = Scheduler(
        selected, [provider(settings, listing)], store, ConsoleNotifier(), clock=lambda: 1000
    )

    assert scheduler.run_once() == []

    assert store.run_state("rainbow") == {"next_run": 1100, "failures": 0}
    assert store.pending() == []


DETAIL_HTML = (Path(__file__).parent / "fixtures/rainbow/detail.html").read_text(encoding="utf-8")
DETAIL_PRODUCT = "6466_12682:249522:10474247"
DETAIL_OPAQUE = "AkIZAooxA7LOAwEBAwfTnwEBAjE5ASMBAgEBAQEBAQABAgNm/REBAgOX/REBAgECAQc="


def diagnostic_filters(settings: Settings) -> FilterConfig:
    """Only this offline check exceeds the production cap; Gardenia's real PLN 1551
    is above the production PLN 1500 cap and would never reach detail enrichment there."""
    filters = deepcopy(settings.filters)
    filters["max_price"] = "2000"
    filters["provider_ratings"]["rainbow"]["price_bands"] = [
        *filters["provider_ratings"]["rainbow"]["price_bands"],
        {"min_price": "1500", "max_price": "2000", "min_rating": 5, "max_inclusive": True},
    ]
    filters["board_price_bands"] = [
        *filters["board_price_bands"],
        {"min_price": "1500", "max_price": "2000", "min_board": "HB", "max_inclusive": True},
    ]
    return filters


def test_enrichment_confirms_a_single_configuration_end_to_end(settings: Settings) -> None:
    # Arrange: the offline evidence chain documented in VARIANT_FINDINGS.md, joining the
    # ambiguous listing card to the one confirmed detail-page configuration.
    evidence = ListingEvidence(
        DETAIL_PRODUCT,
        DETAIL_OPAQUE,
        f"https://r.pl/turcja-riwiera-wczasy/gardenia-hotel?unikalnyKluczOferty={DETAIL_OPAQUE}",
        ("1990-01-01", "1990-01-01"),
        date(2026, 12, 5),
        8,
        7,
        Decimal("1551"),
        ("KTW", "WAW"),
        ("HB", "AI"),
    )

    class DetailListing(FakeListing):
        def evidence(self, offer: Offer) -> ListingEvidence | None:
            return evidence

        def detail_html(self, url: str) -> str:
            assert url == evidence.url
            return DETAIL_HTML

    listing = DetailListing([HTML])
    config: ProviderConfig = {
        "enabled": True,
        "interval_seconds": 3600,
        "max_offers": 1,
        "max_analyzed_cards": 15,
    }

    # Act.
    result = RainbowProvider(
        config, diagnostic_filters(settings), listing.open, lambda: NOW
    ).fetch()

    # Assert: the ambiguous card is narrowed to exactly the confirmed configuration.
    assert len(result) == 1
    enriched = result[0]
    assert enriched.variant_verified is True
    assert enriched.departure_airport == "KTW"
    assert enriched.board_type == "HB"
    assert enriched.return_date == date(2026, 12, 12)
    assert enriched.total_price == Decimal("3102")
    # A selected calculator quote never claims complete mandatory costs.
    assert enriched.price_is_complete is False
    assert enriched.booking_total_price is None
    assert not matches(enriched, settings.filters, NOW.date())


def test_enrichment_mismatch_leaves_offer_unverified_without_raising(settings: Settings) -> None:
    # Arrange: claimed evidence that disagrees with the actual saved detail document.
    wrong_evidence = ListingEvidence(
        "wrong-product-key",
        "wrong-opaque-key",
        "https://r.pl/turcja-riwiera-wczasy/gardenia-hotel?unikalnyKluczOferty=wrong-opaque-key",
        ("1990-01-01", "1990-01-01"),
        date(2026, 12, 5),
        8,
        7,
        Decimal("1551"),
        ("KTW", "WAW"),
        ("HB", "AI"),
    )

    class DetailListing(FakeListing):
        def evidence(self, offer: Offer) -> ListingEvidence | None:
            return wrong_evidence

        def detail_html(self, url: str) -> str:
            return DETAIL_HTML

    listing = DetailListing([HTML])
    config: ProviderConfig = {
        "enabled": True,
        "interval_seconds": 3600,
        "max_offers": 1,
        "max_analyzed_cards": 15,
    }

    # Act.
    result = RainbowProvider(
        config, diagnostic_filters(settings), listing.open, lambda: NOW
    ).fetch()

    # Assert: never a crash or a fabricated verified configuration from disagreeing evidence.
    assert len(result) == 1
    offer = result[0]
    assert offer.variant_verified is False
    assert offer.price_verification_reason is not None
    assert not matches(offer, settings.filters, NOW.date())
