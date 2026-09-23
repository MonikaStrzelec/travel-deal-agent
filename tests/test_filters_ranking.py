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
    "country,stars,expected",
    [
        ("GR", 2, False),
        ("GR", 3, True),
        ("EG", 3, False),
        ("EG", 4, True),
        ("TN", 3, False),
        ("ZA", 4, True),
    ],
)
def test_star_rules(
    offer: Offer, settings: Settings, country: str, stars: int, expected: bool
) -> None:
    assert matches(replace(offer, country=country, hotel_stars=stars), settings.filters) is expected


@pytest.mark.parametrize("country", ["EG", "TN", "ZA", "KE", "MA"])
def test_all_african_destinations_require_four_stars(
    offer: Offer, settings: Settings, country: str
) -> None:
    assert not matches(replace(offer, country=country, hotel_stars=3), settings.filters)


@pytest.mark.parametrize(
    "airport,expected",
    [("LCJ", True), ("WAW", True), ("WMI", True), ("KTW", True), ("WRO", True), ("KRK", False)],
)
def test_airports(offer: Offer, settings: Settings, airport: str, expected: bool) -> None:
    assert matches(replace(offer, departure_airport=airport), settings.filters) is expected


@pytest.mark.parametrize(
    "days,expected",
    [
        (2, False),
        (3, False),
        (6, False),
        (7, True),
        (9, True),
        (10, False),
        (14, False),
        (90, False),
    ],
)
def test_duration(offer: Offer, settings: Settings, days: int, expected: bool) -> None:
    assert matches(replace(offer, number_of_days=days), settings.filters) is expected


def test_short_trip_rejected_even_with_best_price_and_rating(
    offer: Offer, settings: Settings
) -> None:
    assert not matches(
        replace(offer, number_of_days=6, price_per_person=Decimal("100"), rating=10, hotel_stars=5),
        settings.filters,
    )


def test_budget_is_per_person_for_two_travelers(offer: Offer, settings: Settings) -> None:
    candidate = replace(
        offer, price_per_person=Decimal("1500"), total_price=Decimal("3000"), number_of_people=2
    )
    assert matches(candidate, settings.filters)
    assert not matches(replace(candidate, number_of_people=1), settings.filters)
    assert not matches(replace(candidate, number_of_people=3), settings.filters)


def test_optional_duration_limit(offer: Offer, settings: Settings) -> None:
    filters: FilterConfig = {**settings.filters, "max_days": 14}
    assert not matches(replace(offer, number_of_days=15), filters)
    filters["max_days"] = None
    assert matches(replace(offer, number_of_days=15), filters)


@pytest.mark.parametrize(
    "remove_field",
    [
        lambda o: replace(o, price_per_person=None),
        lambda o: replace(o, country=None),
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
