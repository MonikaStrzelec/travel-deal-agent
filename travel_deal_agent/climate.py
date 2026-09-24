"""Static Climate V0: typical daytime temperature for the notification's ``sun`` line.

Every number here is a **typical / average daily maximum temperature** for a
calendar month -- the climatological normal of the daily high, not:

- a weather forecast (this project never calls a live weather API),
- the 24-hour mean temperature,
- a nighttime/overnight temperature,
- a "feels like" value.

Region-level only: `country` alone is not a reliable proxy for climate (see
`SAFE_COUNTRY_FALLBACK` below -- e.g. Spain spans both the seasonal
Mediterranean coast and the mild, near-constant Canary Islands under the same
ISO code). Matching is deliberately simple and explicit: a fixed alias list
per canonical region, matched as whole words/phrases against `Offer.destination`
after normalization -- no fuzzy matching, no NLP, no similarity scoring. An
unrecognized destination and an unmapped country both resolve to `None`;
callers must never guess or placeholder in that case (see
`notification_content.py`, which simply omits the line).
"""

import re
import unicodedata

from typing_extensions import NotRequired, TypedDict

# One canonical region per genuinely distinct climate found among this
# project's real observed destinations (data/offers.sqlite3) and provider
# fixtures (tests/fixtures/{itaka,rainbow}) as of the Climate V0 audit.
# Values are whole-degree averages; see CLIMATE_SOURCES for provenance,
# including the handful of non-standard reference periods and the two
# regions that needed a second source.
CLIMATE_DAYTIME_MAX_C: dict[str, tuple[int, ...]] = {
    "turkey_antalya_riviera": (15, 16, 19, 22, 26, 32, 35, 35, 32, 27, 22, 17),
    "turkey_aegean_coast": (12, 13, 17, 21, 27, 31, 34, 34, 30, 25, 19, 14),
    "egypt_hurghada": (22, 24, 26, 30, 34, 36, 37, 38, 35, 32, 28, 24),
    "egypt_marsa_alam": (23, 24, 27, 30, 32, 35, 35, 35, 34, 32, 27, 25),
    "greece_crete": (15, 16, 17, 20, 24, 28, 29, 29, 27, 24, 20, 17),
    "greece_rhodes": (15, 15, 17, 20, 24, 28, 30, 31, 28, 25, 21, 17),
    "greece_kos": (14, 14, 16, 19, 24, 29, 31, 31, 28, 24, 19, 15),
    "greece_zakynthos": (14, 15, 17, 20, 25, 30, 32, 33, 28, 24, 19, 16),
    "greece_corfu": (14, 14, 17, 20, 25, 29, 32, 33, 28, 24, 19, 15),
    "greece_evia": (13, 14, 17, 20, 26, 30, 32, 32, 28, 24, 19, 14),
    "bulgaria_black_sea_coast": (7, 9, 12, 16, 22, 27, 29, 29, 25, 19, 14, 8),
    "cyprus_east_coast": (17, 18, 20, 23, 27, 31, 33, 33, 31, 28, 23, 19),
    "malta": (16, 16, 17, 20, 24, 29, 32, 32, 29, 25, 21, 17),
    "albania_riviera": (13, 15, 18, 21, 25, 30, 33, 34, 29, 24, 20, 15),
    "tunisia_north_east_coast": (17, 17, 20, 22, 25, 30, 33, 33, 30, 26, 22, 18),
    "tunisia_djerba": (17, 18, 21, 24, 27, 31, 33, 34, 32, 28, 23, 18),
    "spain_costa_dorada": (15, 15, 18, 20, 23, 27, 30, 30, 28, 24, 19, 16),
    "spain_costa_del_sol": (17, 18, 20, 22, 25, 29, 31, 32, 29, 24, 20, 18),
    "spain_costa_blanca": (17, 18, 20, 22, 24, 28, 31, 31, 29, 25, 21, 18),
    "spain_canary_islands": (21, 21, 22, 23, 25, 26, 28, 28, 28, 27, 24, 22),
    "cape_verde_sal": (25, 25, 25, 26, 26, 27, 28, 30, 30, 30, 28, 26),
    "croatia_adriatic_coast": (10, 10, 14, 18, 23, 27, 30, 30, 24, 19, 14, 11),
    "morocco_agadir": (21, 22, 23, 23, 24, 25, 26, 27, 26, 26, 24, 22),
}


class ClimateSource(TypedDict):
    """Provenance metadata only -- never shown in the notification itself,
    kept for future maintenance of CLIMATE_DAYTIME_MAX_C."""

    reference_location: str
    source: str
    reference_period: str
    notes: NotRequired[str]


# Not a runtime dependency of `typical_daytime_temperature`; kept purely so a
# future maintainer can see where each row came from and how confident to be
# in it. All 23 rows were researched from public climate-normal aggregators
# (primarily climatestotravel.com, an aggregator -- not a national met
# service or NOAA/WMO) and cross-checked for internal consistency (e.g.
# Hurghada vs. Marsa Alam, Antalya vs. Izmir, Canary Islands vs. mainland
# Spain) before being accepted as Climate V0.
CLIMATE_SOURCES: dict[str, ClimateSource] = {
    "turkey_antalya_riviera": ClimateSource(
        reference_location="Antalya",
        source="Climates to Travel (climatestotravel.com/climate/turkey/antalya)",
        reference_period="1991-2020",
    ),
    "turkey_aegean_coast": ClimateSource(
        reference_location="Izmir",
        source="Climates to Travel (climatestotravel.com/climate/turkey/izmir)",
        reference_period="1991-2020",
    ),
    "egypt_hurghada": ClimateSource(
        reference_location="Hurghada",
        source="Climates to Travel (climatestotravel.com/climate/egypt/hurghada)",
        reference_period="1991-2020",
    ),
    "egypt_marsa_alam": ClimateSource(
        reference_location="Marsa Alam",
        source=(
            "Climates to Travel (climatestotravel.com/climate/egypt/marsa-alam), "
            "cross-checked against weather-and-climate.com and Weather Spark"
        ),
        reference_period="not stated on primary page",
        notes=(
            "Weakest single-source row of the 23: the primary page gives no "
            "explicit reference period, so it was cross-checked against two "
            "independent sources (incl. an unrelated aggregator) to confirm "
            "the values are a genuine Marsa Alam lookup, not a copy of Hurghada."
        ),
    ),
    "greece_crete": ClimateSource(
        reference_location="Heraklion",
        source="Climates to Travel (climatestotravel.com/climate/greece/crete)",
        reference_period="1991-2020",
    ),
    "greece_rhodes": ClimateSource(
        reference_location="Rhodes",
        source="Climates to Travel (climatestotravel.com/climate/greece/rhodes)",
        reference_period="1991-2020",
    ),
    "greece_kos": ClimateSource(
        reference_location="Kos",
        source="Climates to Travel (climatestotravel.com/climate/greece/kos)",
        reference_period="1991-2020",
    ),
    "greece_zakynthos": ClimateSource(
        reference_location="Zakynthos",
        source="Climates to Travel (climatestotravel.com/climate/greece/zakynthos)",
        reference_period="1991-2020",
    ),
    "greece_corfu": ClimateSource(
        reference_location="Corfu",
        source="Climates to Travel (climatestotravel.com/climate/greece/corfu)",
        reference_period="1991-2020",
    ),
    "greece_evia": ClimateSource(
        reference_location="Chalkida",
        source=(
            "weather-and-climate.com (Copernicus C3S climate averages); "
            "climatestotravel.com has no Evia/Chalkida page"
        ),
        reference_period="1991-2020 (stated by source; reanalysis/gridded, not a ground station)",
        notes="Only region of the 23 with no data at all from the primary source.",
    ),
    "bulgaria_black_sea_coast": ClimateSource(
        reference_location="Burgas",
        source="Climates to Travel (climatestotravel.com/climate/bulgaria/burgas)",
        reference_period="1991-2020",
    ),
    "cyprus_east_coast": ClimateSource(
        reference_location="Larnaca",
        source="Climates to Travel (climatestotravel.com/climate/cyprus)",
        reference_period="1991-2020",
    ),
    "malta": ClimateSource(
        reference_location="Valletta / Luqa",
        source="Climates to Travel (climatestotravel.com/climate/malta)",
        reference_period="1991-2020",
    ),
    "albania_riviera": ClimateSource(
        reference_location="Durres",
        source="Climates to Travel (climatestotravel.com/climate/albania/durres)",
        reference_period="2011-2020",
        notes="Shorter window than the ~30-year normal used for most other rows.",
    ),
    "tunisia_north_east_coast": ClimateSource(
        reference_location="Monastir",
        source="Climates to Travel (climatestotravel.com/climate/tunisia/monastir)",
        reference_period="1991-2020",
    ),
    "tunisia_djerba": ClimateSource(
        reference_location="Djerba",
        source="Climates to Travel (climatestotravel.com/climate/tunisia/djerba)",
        reference_period="1991-2020",
    ),
    "spain_costa_dorada": ClimateSource(
        reference_location="Tarragona",
        source="Climates to Travel (climatestotravel.com/climate/spain/tarragona)",
        reference_period="2009-2019",
        notes="Shorter window than the ~30-year normal used for most other rows.",
    ),
    "spain_costa_del_sol": ClimateSource(
        reference_location="Malaga",
        source="Climates to Travel (climatestotravel.com/climate/spain/malaga)",
        reference_period="1991-2020",
    ),
    "spain_costa_blanca": ClimateSource(
        reference_location="Alicante",
        source="Climates to Travel (climatestotravel.com/climate/spain/alicante)",
        reference_period="1991-2020",
    ),
    "spain_canary_islands": ClimateSource(
        reference_location="Fuerteventura",
        source="Climates to Travel (climatestotravel.com/climate/canary-islands/fuerteventura)",
        reference_period="1991-2020",
        notes=(
            "Narrow ~7C annual range vs. ~15C for the mainland Spain rows -- "
            "confirms this is a genuinely distinct, subtropical-oceanic "
            "climate, not a copy of a mainland Spain row."
        ),
    ),
    "cape_verde_sal": ClimateSource(
        reference_location="Sal Island",
        source="Climates to Travel (climatestotravel.com/climate/cape-verde/sal)",
        reference_period="1991-2020",
    ),
    "croatia_adriatic_coast": ClimateSource(
        reference_location="Rijeka",
        source="Climates to Travel (climatestotravel.com/climate/croatia/rijeka)",
        reference_period="1991-2018",
        notes=(
            "Rijeka (mainland, ~20km from Krk) used as a proxy: the primary "
            "source has no dedicated Krk-island page. Same Kvarner Gulf air "
            "mass, but not an on-island station."
        ),
    ),
    "morocco_agadir": ClimateSource(
        reference_location="Agadir",
        source="Climates to Travel (climatestotravel.com/climate/morocco/agadir)",
        reference_period="1981-2010",
        notes="Shorter/older window than the ~30-year normal used for most other rows.",
    ),
}

# Only for countries where every touristic destination this project has ever
# observed sits in one climatically narrow, uniform band -- see the Climate
# V0 audit for the per-country reasoning. Deliberately excludes TR, GR, EG,
# ES, TN, HR, MA and CV: each spans (or, for HR/MA/CV, has too little
# observed data to rule out) more than one meaningfully different climate
# under the same ISO code -- a single number there would be false precision,
# not a helpful default.
SAFE_COUNTRY_FALLBACK: dict[str, str] = {
    "MT": "malta",
    "AL": "albania_riviera",
    "CY": "cyprus_east_coast",
    "BG": "bulgaria_black_sea_coast",
}

# Explicit alias keywords per canonical region, matched as whole words/phrases
# against the normalized `destination` text -- never a substring search, so a
# short keyword (e.g. "kos", "sal") can't accidentally match inside an
# unrelated longer word (e.g. "Salou"). Country-name aliases are deliberately
# never added here for countries without a safe fallback (TR, GR, EG, ES, TN,
# HR, MA, CV): that would silently reintroduce a country-level guess through
# the back door. Order does not affect correctness (keywords across regions
# don't collide), but is grouped by country for readability.
REGION_ALIASES: dict[str, tuple[str, ...]] = {
    "turkey_antalya_riviera": (
        "riwiera turecka",
        "alanya",
        "avsallar",
        "belek",
        "cengerkoy",
        "colakli",
        "finike",
        "goynuk",
        "incekum",
        "kemer",
        "kiris",
        "konakli",
        "kundu",
        "lara",
        "mahmutlar",
        "manavgat",
        "obagol",
        "okurcalar",
        "side",
        "titreyengol",
    ),
    "turkey_aegean_coast": (
        "wybrzeże egejskie",
        "didim",
        "kusadasi",
        "ozdere",
        "turgutreis",
    ),
    "egypt_hurghada": ("hurghada", "makadi bay", "soma bay"),
    "egypt_marsa_alam": (
        "marsa el alam",
        "marsa alam",
        "al kusajr",
        "madinat coraya",
        "port ghalib",
    ),
    "greece_crete": ("kreta",),
    "greece_rhodes": ("rodos",),
    "greece_kos": ("kos",),
    "greece_zakynthos": ("zakynthos",),
    "greece_corfu": ("korfu",),
    "greece_evia": ("evia",),
    "bulgaria_black_sea_coast": (
        "słoneczny brzeg",
        "riwiera bułgarska",
        "albena",
        "konstantyn i elena",
    ),
    "cyprus_east_coast": ("protaras", "famagusta", "gazimagusa"),
    "malta": ("malta", "bugibba", "mellieha", "st pauls"),
    "albania_riviera": ("riwiera albańska", "durres"),
    "tunisia_north_east_coast": ("hammamet", "monastir", "mahdia", "al mahdijja"),
    "tunisia_djerba": ("djerba", "midoun"),
    "spain_costa_dorada": ("costa dorada", "salou"),
    "spain_costa_del_sol": ("costa del sol", "benalmadena"),
    "spain_costa_blanca": ("costa blanca", "benidorm"),
    "spain_canary_islands": ("wyspy kanaryjskie", "fuerteventura"),
    "cape_verde_sal": ("wyspy zielonego przylądka", "sal"),
    "croatia_adriatic_coast": ("krk", "njivice"),
    "morocco_agadir": ("agadir",),
}

_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _normalize(text: str) -> str:
    """Casefold and collapse whitespace/punctuation for alias matching.

    Mirrors `models.duplicate_key`'s NFKC-plus-casefold strategy. Polish
    diacritics are kept as-is rather than stripped: Unicode NFKD does not
    decompose "l" (it has no accent to remove), so a real diacritic-free
    normalization would need a hand-written transliteration table -- not
    worth the complexity when every alias below is simply written with the
    same diacritics the providers themselves use.
    """
    normalized = unicodedata.normalize("NFKC", text)
    despaced = _PUNCTUATION_RE.sub(" ", normalized)
    return " ".join(despaced.casefold().split())


def _match_region(destination: str | None) -> str | None:
    if not destination:
        return None
    normalized = _normalize(destination)
    for region, keywords in REGION_ALIASES.items():
        for keyword in keywords:
            if re.search(rf"\b{re.escape(keyword)}\b", normalized):
                return region
    return None


def typical_daytime_temperature(
    country: str | None, destination: str | None, month: int | None
) -> int | None:
    """Typical/average daily maximum temperature (°C) for a departure month.

    Resolution order: (1) an explicit region alias match against
    `destination`, (2) for a handful of climatically narrow countries only,
    a country-level fallback (`SAFE_COUNTRY_FALLBACK`), (3) `None`.

    Returns `None` -- never a guessed number -- when the month is missing or
    out of range, or when neither step resolves a region. Callers must treat
    `None` as "omit the line," not as a reason to fall back to a placeholder.
    """
    if month is None or not 1 <= month <= 12:
        return None
    region = _match_region(destination) or SAFE_COUNTRY_FALLBACK.get(country or "")
    if region is None:
        return None
    return CLIMATE_DAYTIME_MAX_C[region][month - 1]
