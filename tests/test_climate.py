"""Climate V0: explicit region aliases and the safe country-fallback allowlist.

No network access, no fuzzy matching -- see travel_deal_agent/climate.py for
what these numbers mean and why country fallback is restricted to MT/AL/CY/BG.
"""

from travel_deal_agent.climate import typical_daytime_temperature


def test_known_region_returns_correct_month() -> None:
    assert typical_daytime_temperature("EG", "Hurghada / Hurghada", 8) == 38


def test_january_and_december_are_distinct_ends_of_the_table() -> None:
    assert typical_daytime_temperature("TR", "Riwiera Turecka / Side", 1) == 15
    assert typical_daytime_temperature("TR", "Riwiera Turecka / Side", 12) == 17


def test_unrecognized_region_and_no_fallback_country_is_none() -> None:
    assert typical_daytime_temperature("TR", "Nieznana Miejscowość", None) is None


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


def test_turkey_has_no_country_fallback() -> None:
    assert typical_daytime_temperature("TR", None, 7) is None
    assert typical_daytime_temperature("TR", "Some Unmapped Town", 7) is None


def test_malta_falls_back_to_country_when_destination_is_missing() -> None:
    assert typical_daytime_temperature("MT", None, 1) == 16


def test_albania_falls_back_to_country_when_destination_is_missing() -> None:
    assert typical_daytime_temperature("AL", None, 10) == 24


def test_cyprus_and_bulgaria_country_fallback() -> None:
    assert typical_daytime_temperature("CY", "Unmapped Cyprus Resort", 6) == 31
    assert typical_daytime_temperature("BG", "Unmapped Bulgaria Resort", 6) == 27


def test_croatia_and_morocco_have_no_country_fallback() -> None:
    assert typical_daytime_temperature("HR", "Unmapped Croatian Town", 7) is None
    assert typical_daytime_temperature("MA", "Unmapped Moroccan Town", 7) is None


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
