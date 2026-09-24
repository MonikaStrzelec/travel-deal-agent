"""Independent business-rule examples, including the country-independent star rule."""

from dataclasses import replace
from decimal import Decimal

import pytest

from travel_deal_agent.boards import normalize_board
from travel_deal_agent.config import Settings
from travel_deal_agent.filtering import matches
from travel_deal_agent.models import Offer
from travel_deal_agent.ranking import score

# Independent reference list: 54 African sovereign states, the countries that
# previously required 4 stars, and destinations that were never on any whitelist.
COUNTRIES = [
    "DZ",
    "AO",
    "BJ",
    "BW",
    "BF",
    "BI",
    "CV",
    "CM",
    "CF",
    "TD",
    "KM",
    "CG",
    "CD",
    "CI",
    "DJ",
    "EG",
    "GQ",
    "ER",
    "SZ",
    "ET",
    "GA",
    "GM",
    "GH",
    "GN",
    "GW",
    "KE",
    "LS",
    "LR",
    "LY",
    "MG",
    "MW",
    "ML",
    "MR",
    "MU",
    "MA",
    "MZ",
    "NA",
    "NE",
    "NG",
    "RW",
    "ST",
    "SN",
    "SC",
    "SL",
    "SO",
    "ZA",
    "SS",
    "SD",
    "TZ",
    "TG",
    "TN",
    "UG",
    "ZM",
    "ZW",
    "AL",
    "BG",
    "HR",
    "TR",
    "BY",
    "ME",
    "LT",
    "LV",
    "MD",
    "RS",
    "XK",
    "MT",
    "PT",
    "IT",
    "CY",
    "GR",
    "ES",
]


@pytest.mark.parametrize("country", COUNTRIES)
@pytest.mark.parametrize("stars,expected", [(2, False), (3, True), (4, True)])
def test_three_stars_suffice_in_every_country(
    offer: Offer, settings: Settings, country: str, stars: int, expected: bool
) -> None:
    # Arrange
    candidate = replace(offer, country=country, hotel_stars=stars)
    # Act / Assert
    assert matches(candidate, settings.filters) is expected


def test_no_country_specific_star_override_exists(settings: Settings) -> None:
    # Arrange / Act / Assert
    assert "country_min_stars" not in settings.filters
    assert settings.filters["min_stars"] == 3


def test_star_minimum_is_configuration(offer: Offer, settings: Settings) -> None:
    # Arrange
    candidate = replace(offer, country="EG", hotel_stars=3)
    # Act / Assert
    assert matches(candidate, settings.filters)
    settings.filters["min_stars"] = 4
    assert not matches(candidate, settings.filters)
    assert not matches(replace(candidate, country="GR"), settings.filters)


def test_unmapped_country_is_not_rejected(offer: Offer, settings: Settings) -> None:
    # Arrange: a source country that no provider mapping knows yet.
    candidate = replace(offer, country=None, hotel_stars=3)
    # Act / Assert
    assert matches(candidate, settings.filters)


@pytest.mark.parametrize("country", ["gr", "GRC", "", "Malta"])
def test_malformed_country_code_is_still_rejected(
    offer: Offer, settings: Settings, country: str
) -> None:
    # Arrange
    candidate = replace(offer, country=country)
    # Act / Assert
    assert not matches(candidate, settings.filters)


@pytest.mark.parametrize(
    "meal", ["BB", "breakfast", "bed & breakfast", "śniadanie", "HB", "FB", "AI", "UAI"]
)
def test_breakfast_or_better(offer: Offer, settings: Settings, meal: str) -> None:
    # Exercise configurable breakfast-inclusive policy, independent of the stricter default.
    settings.filters["allowed_boards"] = ["BB", "HB", "FB", "AI", "UAI"]
    # Arrange / Act / Assert
    assert matches(
        replace(offer, board_type=meal, price_per_person=Decimal("999.99")), settings.filters
    )


@pytest.mark.parametrize(
    "meal",
    [
        None,
        "",
        "unknown",
        "BB or RO",
        "RO",
        "room only",
        "self catering",
        "bez wyżywienia",
        "własne wyżywienie",
        "breakfast available at extra charge",
    ],
)
def test_missing_meals_rejected(offer: Offer, settings: Settings, meal: str | None) -> None:
    # Arrange / Act / Assert
    assert not matches(replace(offer, board_type=meal), settings.filters)


def test_meal_order_and_airport_tie(offer: Offer, settings: Settings) -> None:
    # Arrange
    scores = [
        score(replace(offer, board_type=b), settings.ranking, "1500")
        for b in ("BB", "HB", "FB", "AI", "UAI")
    ]
    # Act / Assert
    assert all(a < b for a, b in zip(scores, scores[1:], strict=False))
    assert score(replace(offer, departure_airport="WAW"), settings.ranking, "1500") == score(
        replace(offer, departure_airport="WMI"), settings.ranking, "1500"
    )


@pytest.mark.parametrize(
    "title,code,expected",
    [
        ("All inclusive", "A", "AI"),
        ("HB", "H", "HB"),
        ("Breakfast", "A", None),
        ("BB", "X", None),
        ("Breakfast", "X", "BB"),
        (None, "A", None),
    ],
)
def test_itaka_code_requires_consistent_name(
    title: str | None, code: str, expected: str | None
) -> None:
    # Arrange / Act / Assert
    assert normalize_board("itaka", title, code) == expected


@pytest.mark.parametrize(
    "price,board,expected",
    [
        ("999.99", "BB", True),
        ("1000", "BB", False),
        ("1000", "HB", True),
        ("1500", "HB", True),
        ("1500.01", "HB", False),
    ],
)
def test_meal_price_boundaries(
    offer: Offer, settings: Settings, price: str, board: str, expected: bool
) -> None:
    # Arrange
    settings.filters["allowed_boards"] = ["BB", "HB", "FB", "AI", "UAI"]
    final_price = Decimal(price)
    candidate = replace(
        offer, price_per_person=final_price, total_price=2 * final_price, board_type=board
    )
    # Act / Assert
    assert matches(candidate, settings.filters) is expected


@pytest.mark.parametrize("price", ["1", "999.99", "1000", "1500", "1500.01"])
@pytest.mark.parametrize("board", ["RO", "room only", "self catering", None, "unknown"])
def test_no_meals_never_pass(
    offer: Offer, settings: Settings, price: str, board: str | None
) -> None:
    # Arrange / Act / Assert
    assert not matches(
        replace(offer, price_per_person=Decimal(price), board_type=board), settings.filters
    )


@pytest.mark.parametrize("price", ["999.99", "1000", "1500"])
@pytest.mark.parametrize("board", ["FB", "AI", "UAI"])
def test_higher_meals_pass_all_budget_bands(
    offer: Offer, settings: Settings, price: str, board: str
) -> None:
    settings.filters["allowed_boards"] = ["BB", "HB", "FB", "AI", "UAI"]
    # Arrange / Act / Assert
    assert matches(
        replace(offer, price_per_person=Decimal(price), board_type=board), settings.filters
    )


@pytest.mark.parametrize("complete", [False, True])
def test_mandatory_fees_move_offer_to_higher_meal_band(
    offer: Offer, settings: Settings, complete: bool
) -> None:
    # Arrange: the source-normalized final price includes both mandatory fees.
    base = Decimal("980")
    final_price = base + Decimal("10") + Decimal("10")
    candidate = replace(
        offer,
        price_per_person=final_price,
        total_price=final_price * 2,
        price_is_complete=complete,
        board_type="BB",
    )
    # Act / Assert
    assert not matches(candidate, settings.filters)
    assert matches(replace(candidate, board_type="HB"), settings.filters) is complete


def test_meal_threshold_is_configurable(offer: Offer, settings: Settings) -> None:
    # Arrange
    settings.filters["allowed_boards"] = ["BB", "HB", "FB", "AI", "UAI"]
    candidate = replace(offer, price_per_person=Decimal("950"), board_type="BB")
    assert matches(candidate, settings.filters)
    settings.filters["board_price_bands"][0]["max_price"] = "900"
    settings.filters["board_price_bands"][1]["min_price"] = "900"
    # Act / Assert
    assert not matches(candidate, settings.filters)


@pytest.mark.parametrize(
    "mutation", ["gap", "overlap", "empty", "ro", "unknown", "nan", "exclusive_end"]
)
def test_invalid_meal_bands_rejected(settings: Settings, mutation: str) -> None:
    from travel_deal_agent.config import validate_filters

    # Arrange
    bands = settings.filters["board_price_bands"]
    if mutation == "gap":
        bands[1]["min_price"] = "1001"
    elif mutation == "overlap":
        bands[0]["max_inclusive"] = True
    elif mutation == "empty":
        bands.clear()
    elif mutation == "ro":
        bands[0]["min_board"] = "RO"
    elif mutation == "unknown":
        bands[0]["min_board"] = "unknown"
    elif mutation == "nan":
        bands[0]["max_price"] = "NaN"
    else:
        bands[1]["max_inclusive"] = False
    # Act / Assert
    with pytest.raises(ValueError):
        validate_filters(settings.filters)


# --- accept_incomplete_price_from: per-provider price-completeness exemption --------


def test_whitelisted_provider_with_incomplete_price_still_matches(
    offer: Offer, settings: Settings
) -> None:
    # Arrange: config.json whitelists wakacje.pl; an otherwise fully matching
    # offer with an unconfirmed (listing-only) price must still be eligible.
    candidate = replace(offer, provider="wakacje.pl", price_is_complete=False, rating=8.4)
    # Act / Assert
    assert matches(candidate, settings.filters)


def test_non_whitelisted_provider_with_incomplete_price_never_matches(
    offer: Offer, settings: Settings
) -> None:
    # Arrange: "mock" is not in accept_incomplete_price_from -- unchanged, original
    # behavior: an incomplete price always disqualifies it.
    candidate = replace(offer, price_is_complete=False)
    # Act / Assert
    assert not matches(candidate, settings.filters)


def test_whitelist_is_strictly_per_provider(offer: Offer, settings: Settings) -> None:
    # Arrange: whitelisting wakacje.pl must not accidentally loosen any other
    # provider's requirement, even one otherwise identical in every other field.
    candidate = replace(offer, provider="itaka", price_is_complete=False, rating=5.5)
    # Act / Assert
    assert not matches(candidate, settings.filters)


def test_empty_whitelist_reproduces_the_original_unconditional_requirement(
    offer: Offer, settings: Settings
) -> None:
    # Arrange
    settings.filters["accept_incomplete_price_from"] = []
    candidate = replace(offer, provider="wakacje.pl", price_is_complete=False, rating=8.4)
    # Act / Assert
    assert not matches(candidate, settings.filters)
