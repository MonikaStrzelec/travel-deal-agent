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
        # TZ and OM each span climatically unrelated destinations under the
        # same code (Zanzibar coast vs. mainland safari; Salalah vs. Muscat)
        # -- no country fallback, and a bare country code alone must stay None.
        ("TZ", None, 7),
        ("OM", "Muscat", 7),
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


@pytest.mark.parametrize(
    "country,destination",
    [
        # Real Wakacje.pl shape: no country code, Polish names with diacritics.
        (None, "Czarnogóra / Riwiera Czarnogórska / Budva"),
        (None, "Montenegro / Becici"),
        ("ME", "Petrovac"),
        # ITAKA maps Montenegro to ME; an unmapped town uses the safe fallback.
        ("ME", "Unmapped Montenegrin Resort"),
        ("ME", None),
    ],
)
def test_montenegro_resolves_to_coast(country: str | None, destination: str | None) -> None:
    assert typical_daytime_temperature(country, destination, 7) == 30
    assert typical_daytime_temperature(country, destination, 1) == 13


@pytest.mark.parametrize(
    "destination",
    ["Majorka / Puerto De Alcudia", "Mallorca", "Hiszpania / Majorka / Cala Millor"],
)
def test_mallorca_resolves_to_its_own_region(destination: str) -> None:
    assert typical_daytime_temperature("ES", destination, 7) == 31
    assert typical_daytime_temperature("ES", destination, 1) == 16


def test_mallorca_is_distinct_from_canary_islands() -> None:
    assert typical_daytime_temperature("ES", "Mallorca", 1) != typical_daytime_temperature(
        "ES", "Fuerteventura", 1
    )


@pytest.mark.parametrize("destination", ["Gran Canaria", "Teneryfa", "Lanzarote", "La Palma"])
def test_other_canary_islands_resolve_to_canary_islands(destination: str) -> None:
    assert typical_daytime_temperature("ES", destination, 1) == 21


def test_bodrum_resolves_to_aegean_coast() -> None:
    assert typical_daytime_temperature("TR", "Bodrum", 7) == 34


def test_spain_mainland_unmapped_resort_stays_none() -> None:
    assert typical_daytime_temperature("ES", "Hiszpania / Costa de la Luz / Unmapped", 7) is None


@pytest.mark.parametrize(
    "destination",
    [
        "Hiszpania / Costa Brava / Lloret de Mar",
        "Hiszpania / Costa Brava / Calella",
        "Costa Brava / Tossa de Mar",
    ],
)
def test_costa_brava_resolves_by_real_provider_shapes(destination: str) -> None:
    assert typical_daytime_temperature("ES", destination, 7) == 31
    assert typical_daytime_temperature("ES", destination, 1) == 14


def test_agadir_alias() -> None:
    assert typical_daytime_temperature(None, "Maroko / Agadir / Agadir", 7) == 26


def test_marrakesh_is_its_own_inland_region_not_agadir() -> None:
    assert typical_daytime_temperature("MA", "Maroko / Marrakesz-Safi / Marrakesz", 7) == 38
    assert typical_daytime_temperature("MA", "Marrakesz", 7) != typical_daytime_temperature(
        "MA", "Agadir", 7
    )


def test_sharm_el_sheikh_is_its_own_region_not_hurghada() -> None:
    assert typical_daytime_temperature("EG", "Egipt / Sharm el Sheikh / Ras Um Sid", 8) == 38
    assert typical_daytime_temperature("EG", "Sharm El Sheikh", 6) != typical_daytime_temperature(
        "EG", "Hurghada", 6
    )


@pytest.mark.parametrize("destination", ["Portugalia / Madera / Machico", "Madeira", "Funchal"])
def test_madeira_alias(destination: str) -> None:
    assert typical_daytime_temperature("PT", destination, 1) == 20
    assert typical_daytime_temperature("PT", destination, 8) == 27


def test_vodice_resolves_to_central_dalmatia_not_kvarner() -> None:
    assert typical_daytime_temperature(None, "Chorwacja / Dalmacja Północna / Vodice", 7) == 31
    # Meaningfully different from the Kvarner Gulf (Krk) row, not a copy.
    assert typical_daytime_temperature(
        None, "Chorwacja / Dalmacja Północna / Vodice", 11
    ) != typical_daytime_temperature(None, "Chorwacja / Krk / Njivice", 11)


def test_salalah_alias() -> None:
    # Monsoon-cooled summer, unlike most rows -- May must be the peak, not August.
    assert typical_daytime_temperature("OM", "Salalah", 5) == 33
    assert typical_daytime_temperature("OM", "Salalah", 8) == 27


def test_zanzibar_alias_without_tz_country_fallback() -> None:
    assert typical_daytime_temperature("TZ", "Zanzibar", 1) == 33
    # A bare TZ code with no destination text must still stay None (mainland
    # safari destinations share the same code and are climatically unrelated).
    assert typical_daytime_temperature("TZ", None, 1) is None


@pytest.mark.parametrize(
    "destination,expected_july",
    [
        # Polish provider spellings (ITAKA, Wakacje.pl, TUI) and English names.
        ("Kreta / Kolymbari", 29),
        ("Grecja / Kreta / Plakias", 29),
        ("Crete", 29),
        ("Rodos / Ialyssos", 30),
        ("Rhodes", 30),
        ("Korfu / Moraitika", 32),
        ("Corfu", 32),
        ("Zakynthos / Laganas", 32),
        ("Kos / Mastichari", 31),
        ("Evia / Eretria", 32),
        # Real Wakacje.pl destination that previously had no climate line.
        ("Chalkidiki / Pefkochori", 32),
        ("Halkidiki", 32),
    ],
)
def test_greek_regions_resolve_by_polish_and_english_names(
    destination: str, expected_july: int
) -> None:
    assert typical_daytime_temperature("GR", destination, 7) == expected_july


def test_chalkidiki_is_its_own_region_not_a_copy_of_another_greek_one() -> None:
    assert typical_daytime_temperature("GR", "Chalkidiki / Pefkochori", 1) == 10
    assert typical_daytime_temperature("GR", "Chalkidiki / Pefkochori", 10) == 22


@pytest.mark.parametrize(
    "destination",
    [
        None,
        "Unmapped Greek Village",
        # Similar-looking words must not match a Greek alias by substring.
        "Kosta Rica",
        "Kretinga",
    ],
)
def test_unknown_greek_destination_is_none_not_a_guess(destination: str | None) -> None:
    assert typical_daytime_temperature("GR", destination, 7) is None
