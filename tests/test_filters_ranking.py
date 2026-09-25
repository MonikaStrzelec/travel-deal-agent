from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from travel_deal_agent.config import Settings
from travel_deal_agent.config_types import FilterConfig, RankingConfig
from travel_deal_agent.filtering import matches
from travel_deal_agent.models import Offer, duplicate_key
from travel_deal_agent.ranking import deduplicate, rank_offers


@pytest.mark.parametrize(
    "price,expected", [("1499.99", True), ("1500", True), ("1500.01", False), ("0", False)]
)
def test_price_boundary(offer: Offer, settings: Settings, price: str, expected: bool) -> None:
    assert matches(replace(offer, price_per_person=Decimal(price)), settings.filters) is expected


@pytest.mark.parametrize(
    "airport,expected",
    [("LCJ", True), ("WAW", True), ("WMI", True), ("KTW", True), ("WRO", True), ("KRK", False)],
)
def test_airports(offer: Offer, settings: Settings, airport: str, expected: bool) -> None:
    assert matches(replace(offer, departure_airport=airport), settings.filters) is expected


@pytest.mark.parametrize(
    "nights,expected",
    [
        (2, False),
        (3, False),
        (5, False),
        (6, True),
        (8, True),
        (9, False),
        (14, False),
        (90, False),
    ],
)
def test_duration(offer: Offer, settings: Settings, nights: int, expected: bool) -> None:
    """Stay length is the canonical `return_date - departure_date`, in nights --
    never the provider-specific `number_of_days` (see `test_nights_are_computed_
    from_dates_regardless_of_provider_convention` for the cross-provider proof)."""
    candidate = replace(
        offer,
        return_date=offer.departure_date + timedelta(days=nights),  # type: ignore
    )
    assert matches(candidate, settings.filters) is expected


def test_short_trip_rejected_even_with_best_price_and_rating(
    offer: Offer, settings: Settings
) -> None:
    assert not matches(
        replace(
            offer,
            return_date=offer.departure_date + timedelta(days=5),  # type: ignore
            price_per_person=Decimal("100"),
            rating=10,
            hotel_stars=5,
        ),
        settings.filters,
    )


def test_budget_is_per_person_for_two_travelers(offer: Offer, settings: Settings) -> None:
    candidate = replace(
        offer, price_per_person=Decimal("1500"), total_price=Decimal("3000"), number_of_people=2
    )
    assert matches(candidate, settings.filters)
    assert not matches(replace(candidate, number_of_people=1), settings.filters)
    assert not matches(replace(candidate, number_of_people=3), settings.filters)


@pytest.mark.parametrize(
    "min_nights,max_nights,nights,expected",
    [
        # Both bounds null (production config.json): stay length is unrestricted,
        # so a good 3-, 5-, 10-, 14- or 15-night deal must still be found.
        (None, None, 3, True),
        (None, None, 5, True),
        (None, None, 10, True),
        (None, None, 14, True),
        (None, None, 15, True),
        (None, 14, 15, False),
        (7, None, 6, False),
        (7, None, 7, True),
        (None, 10, 10, True),
        (None, 10, 11, False),
        (7, 10, 6, False),
        (7, 10, 7, True),
        (7, 10, 10, True),
        (7, 10, 11, False),
    ],
)
def test_nights_range_filter(
    offer: Offer,
    settings: Settings,
    min_nights: int | None,
    max_nights: int | None,
    nights: int,
    expected: bool,
) -> None:
    filters: FilterConfig = {**settings.filters, "min_nights": min_nights, "max_nights": max_nights}
    candidate = replace(
        offer,
        return_date=offer.departure_date + timedelta(days=nights),  # type: ignore
    )
    assert matches(candidate, filters) is expected


@pytest.mark.parametrize(
    "provider,native_days",
    [
        # Wakacje.pl/TUI: number_of_days IS the night count.
        ("wakacje.pl", 7),
        ("tui", 7),
        # ITAKA/Rainbow: number_of_days is the touroperator "dni" count, nights + 1.
        ("itaka", 8),
        ("rainbow", 8),
    ],
)
def test_nights_are_computed_from_dates_regardless_of_provider_convention(
    offer: Offer, settings: Settings, provider: str, native_days: int
) -> None:
    """01.10 -> 08.10 is 7 nights for every provider, even though each provider's
    own `number_of_days` disagrees on what that span is called. Provider rating
    rules are disabled here so only duration eligibility is under test."""
    filters: FilterConfig = {
        **settings.filters,
        "min_nights": 7,
        "max_nights": 7,
        "provider_ratings": {
            name: {"enabled": False, "scale": None, "price_bands": []}
            for name in ("wakacje.pl", "tui", "itaka", "rainbow")
        },
    }
    candidate = replace(
        offer,
        provider=provider,
        departure_date=date(2026, 10, 1),
        return_date=date(2026, 10, 8),
        number_of_days=native_days,
    )
    assert matches(candidate, filters, today=date(2026, 1, 1))


@pytest.mark.parametrize(
    "remove_field",
    [
        lambda o: replace(o, price_per_person=None),
        lambda o: replace(o, departure_date=None),
        lambda o: replace(o, return_date=None),
        lambda o: replace(o, number_of_days=None),
        lambda o: replace(o, hotel_stars=None),
        lambda o: replace(o, departure_airport=None),
        lambda o: replace(o, number_of_people=None),
        lambda o: replace(o, currency=None),
    ],
)
def test_missing_filter_data(
    offer: Offer, settings: Settings, remove_field: Callable[[Offer], Offer]
) -> None:
    candidate = remove_field(offer)

    result = matches(candidate, settings.filters)

    assert not result


def test_optional_data_and_people_currency(offer: Offer, settings: Settings) -> None:
    assert matches(replace(offer, rating=None, number_of_reviews=None, url=None), settings.filters)
    assert not matches(replace(offer, number_of_people=1), settings.filters)
    assert not matches(replace(offer, currency="EUR"), settings.filters)
    assert not matches(
        replace(offer, departure_date=date.today() - timedelta(days=1)), settings.filters
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda o: replace(o, price_per_person=Decimal("1100")),
        lambda o: replace(o, departure_airport="LCJ"),
        lambda o: replace(o, rating=9.9),
        lambda o: replace(o, number_of_reviews=900),
        lambda o: replace(o, hotel_stars=5),
    ],
)
def test_ranking_preferences(
    offer: Offer, settings: Settings, change: Callable[[Offer], Offer]
) -> None:
    baseline = replace(offer, departure_airport="WRO")
    improved = replace(change(baseline), offer_id="improved")

    result = rank_offers([baseline, improved], settings.ranking, "1500")

    assert result[0] == improved


def test_configurable_ranking(offer: Offer, settings: Settings) -> None:
    cheap = replace(
        offer, offer_id="cheap", price_per_person=Decimal("1000"), departure_airport="WRO"
    )
    ranking: RankingConfig = {
        **settings.ranking,
        "weights": dict(price=1, airport=0, rating=0, reviews=0, stars=0),
    }
    assert rank_offers([offer, cheap], ranking, "1500")[0] == cheap


def test_duplicate_grouping(offer: Offer) -> None:
    cheaper = replace(
        offer,
        provider="other",
        offer_id="different",
        hotel_name=" SUNNY  DEMO ",
        price_per_person=Decimal("1200"),
    )
    assert duplicate_key(offer) == duplicate_key(cheaper)
    assert deduplicate([offer, offer, cheaper]) == [cheaper]
    assert duplicate_key(replace(offer, board_type="breakfast")) != duplicate_key(offer)
    assert duplicate_key(
        replace(offer, departure_date=(offer.departure_date or date.today()) + timedelta(days=1))
    ) != duplicate_key(offer)
    # A different departure airport alone (same hotel/dates/board/etc.) is a
    # separate trip and must never share price history/alert state with the
    # original -- isolated here from the date-change case above, and from any
    # single provider's own airport-plus-date fixture.
    assert duplicate_key(replace(offer, departure_airport="WAW")) != duplicate_key(
        replace(offer, departure_airport="KTW")
    )


def test_incomplete_duplicates_stay_separate(offer: Offer) -> None:
    assert duplicate_key(replace(offer, hotel_name=None)) != duplicate_key(
        replace(offer, hotel_name=None, provider="other")
    )
    assert Offer("mock", "minimal").rating is None


@pytest.mark.parametrize(
    "change",
    [
        lambda o: replace(o, price_per_person=Decimal("NaN")),
        lambda o: replace(o, price_per_person=Decimal("-1")),
        lambda o: replace(o, rating=float("nan")),
        lambda o: replace(o, number_of_people=0),
        lambda o: replace(o, number_of_reviews=-1),
    ],
)
def test_invalid_model(offer: Offer, change: Callable[[Offer], Offer]) -> None:
    with pytest.raises(ValueError):
        change(offer)
