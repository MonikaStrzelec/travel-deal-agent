"""Climate V0: explicit region aliases and the safe country-fallback allowlist.

No network access, no fuzzy matching -- see travel_deal_agent/climate.py for
what these numbers mean and why country fallback is restricted to MT/AL/CY/BG.
"""

import pytest

from travel_deal_agent.climate import typical_daytime_temperature


def test_known_region_returns_correct_month() -> None:
    assert typical_daytime_temperature("EG", "Hurghada / Hurghada", 8) == 38


def test_january_and_december_are_distinct_ends_of_the_table() -> None:
    assert typical_daytime_temperature("TR", "Riwiera Turecka / Side", 1) == 15
    assert typical_daytime_temperature("TR", "Riwiera Turecka / Side", 12) == 17


def test_month_none_is_none() -> None:
    assert typical_daytime_temperature("EG", "Hurghada", None) is None


def test_invalid_month_is_none() -> None:
    assert typical_daytime_temperature("EG", "Hurghada", 0) is None
    assert typical_daytime_temperature("EG", "Hurghada", 13) is None


def test_alias_matching_is_case_and_punctuation_insensitive() -> None:
    lower = typical_daytime_temperature("EG", "hurghada", 5)
    upper = typical_daytime_temperature("EG", "HURGHADA", 5)
    with_bullet = typical_daytime_temperature("EG", "Wypoczynek • Hurghada", 5)
    assert lower == upper == with_bullet == 34


def test_fuerteventura_resolves_to_canary_islands_not_spain_country_fallback() -> None:
    # Spain has no safe country fallback (see SAFE_COUNTRY_FALLBACK) -- this
    # must resolve purely through the destination alias, and it must give
    # the Canary Islands' mild, narrow-range numbers, not a mainland guess.
    assert typical_daytime_temperature("ES", "Fuerteventura", 1) == 21
    assert typical_daytime_temperature("ES", "Fuerteventura", 1) != typical_daytime_temperature(
        "ES", "Costa del Sol / Benalmadena", 1
    )


@pytest.mark.parametrize(
    "country,destination,month",
    [
        ("TR", None, 7),
        ("TR", "Some Unmapped Town", 7),
        ("HR", "Unmapped Croatian Town", 7),
        ("MA", "Unmapped Moroccan Town", 7),
    ],
)
def test_country_without_fallback_is_none(
    country: str, destination: str | None, month: int
) -> None:
    assert typical_daytime_temperature(country, destination, month) is None


@pytest.mark.parametrize(
    "country,destination,month,expected",
    [
        # Destination missing entirely.
        ("MT", None, 1, 16),
        ("AL", None, 10, 24),
        # Destination present but not mapped to a region.
        ("CY", "Unmapped Cyprus Resort", 6, 31),
        ("BG", "Unmapped Bulgaria Resort", 6, 27),
    ],
)
def test_allowlisted_country_falls_back_to_country_table(
    country: str, destination: str | None, month: int, expected: int
) -> None:
    assert typical_daytime_temperature(country, destination, month) == expected


def test_marsa_alam_and_marsa_el_alam_aliases() -> None:
    assert typical_daytime_temperature("EG", "Marsa Alam", 1) == 23
    assert typical_daytime_temperature("EG", "Marsa El Alam / Al-Kusajr", 1) == 23
    # And it must be genuinely distinct from Hurghada, not a copy.
    assert typical_daytime_temperature("EG", "Marsa Alam", 1) != typical_daytime_temperature(
        "EG", "Hurghada", 1
    )


def test_krk_and_njivice_alias() -> None:
    assert typical_daytime_temperature(None, "Chorwacja / Krk / Njivice", 7) == 30


def test_agadir_alias() -> None:
    assert typical_daytime_temperature(None, "Maroko / Agadir / Agadir", 7) == 26
