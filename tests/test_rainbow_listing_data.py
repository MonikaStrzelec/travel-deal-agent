"""Offline checks for joining captured listing JSON; no browser or network access."""

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from travel_deal_agent.providers.rainbow_data import parse_card
from travel_deal_agent.providers.rainbow_listing_data import (
    ListingEvidence,
    ListingExchange,
    evidence_for,
    listing_json,
    matched_batches,
    product_url,
    search_matches,
)
from travel_deal_agent.providers.rainbow_nuxt import RainbowDetailError

HTML = (Path(__file__).parent / "fixtures/rainbow/card.html").read_text(encoding="utf-8")
NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)
OFFER = parse_card(HTML, NOW)
BIRTHS = ("1990-01-01", "1992-06-15")
ROW = {
    "Id": "KEY-1",
    "UnikalnyKluczOferty": "OPAQUE-1",
    "Cena": 1551,
    "TerminWyjazdu": "2026-12-05T13:13:00Z",
    "LiczbaDni": 8,
}
CARD = {
    "Klucz": "KEY-1",
    "UnikalnyKluczOferty": "OPAQUE-1",
    "BazoweInformacje": {
        "OfertaUrl": (
            "/turcja-riwiera-wczasy/gardenia-hotel"
            "?unikalnyKluczOferty=OPAQUE-1&liczbaPokoi=1&czyCenaZaWszystkich=0"
            "&wiek=1990-01-01&wiek=1992-06-15"
        ),
        "LiczbaDni": 8,
        "LiczbaNocy": 7,
        "GwiazdkiHotelu": 4,
        "NazwaHoteluWWW": "Gardenia Hotel",
        "DatyUrodzenia": list(BIRTHS),
        "LiczbaPokoi": 1,
    },
    "Cena": {"Cena": 1551, "CzyCenaZaOsobe": True},
    "TerminWyjazdu": "2026-12-05T13:13:00Z",
    "Przystanki": [{"Iata": "KTW"}, {"Iata": "WAW"}],
    "Wyzywienia": [{"Nazwa": "2 posiłki"}, {"Nazwa": "All inclusive"}],
}


def expected(**overrides: object) -> dict[str, list[str]]:
    base: dict[str, list[str]] = {
        "wybraneSkad": ["KTW", "WAW"],
        "wyzywienia": ["2-posilki", "all-inclusive"],
        "standardHotelu": ["6", "8", "10"],
        "dlugoscPobytu": ["7-9"],
        "cena.do": ["1500"],
        "ocenaKlientow": ["5.0-*"],
        "dorosli": list(BIRTHS),
        "sortowanie": ["cena-asc"],
    }
    base.update(overrides)  # type: ignore[arg-type]
    return base


def search_request(exp: dict[str, list[str]], strona: int = 1) -> dict[str, object]:
    request: dict[str, object] = {
        "Sortowanie": "cena-asc",
        "DatyUrodzenia": exp["dorosli"],
        "LiczbaPokoi": 1,
        "CzyWeekendowka": False,
        "PowrotNaInneLotnisko": False,
        "CzyPotwierdzone": False,
        "Atrybuty": {
            "Miasta": list(exp["wybraneSkad"]),
            "Wyzywienia": list(exp["wyzywienia"]),
            "StandardHotelu": list(exp["standardHotelu"]),
            "DlugoscPobytu": list(exp["dlugoscPobytu"]),
            "Cena": ["avg", "*-" + exp["cena.do"][0]],
            "OcenaKlientow": list(exp["ocenaKlientow"]),
            "OdlegloscLotnisko": ["*-*"],
            "TypTransportu": ["AIR", "dreamliner"],
            "Lokalizacje_HoteloProdukt": [],
        },
    }
    if strona != 1:
        request["Strona"] = strona
    return request


def search_response(rows: list[dict[str, object]]) -> object:
    return {"Wynik": rows, "CzyCenaZaOsobe": True}


def cards_request(exp: dict[str, list[str]], rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "Parametry": rows,
        "DatyUrodzenia": exp["dorosli"],
        "LiczbaPokoi": 1,
        "CzyCenaZaOsobe": True,
    }


def test_product_url_accepts_relative_r_pl_path() -> None:
    # Arrange / act / assert.
    assert product_url("/turcja-riwiera-wczasy/gardenia-hotel") == (
        "https://r.pl/turcja-riwiera-wczasy/gardenia-hotel"
    )


@pytest.mark.parametrize(
    "value",
    [
        "/api/foo/bar",
        "/szukaj/results",
        "/koszyk/checkout",
        "/rezerwacja/step-1",
        "https://evil.example/turcja-riwiera-wczasy/gardenia-hotel",
        "/turcja-riwiera-wczasy/gardenia-hotel\nX-Injected: 1",
        "/only-one-segment",
    ],
)
def test_product_url_rejects_unexpected_paths(value: str) -> None:
    # Arrange / act / assert.
    with pytest.raises(RainbowDetailError):
        product_url(value)


def test_listing_json_rejects_duplicate_fields() -> None:
    # Arrange / act / assert.
    with pytest.raises(RainbowDetailError):
        listing_json(b'{"a": 1, "a": 2}')


def test_listing_json_rejects_oversized_body() -> None:
    # Arrange / act / assert.
    with pytest.raises(RainbowDetailError):
        listing_json(b"x" * 2_000_001)


def test_search_matches_true_for_expected_filters() -> None:
    # Arrange.
    exp = expected()

    # Act / assert.
    assert search_matches(search_request(exp), exp)


def test_search_matches_false_when_attribute_disagrees() -> None:
    # Arrange.
    exp = expected()
    request = search_request(exp)
    request["Atrybuty"] = {**request["Atrybuty"], "Miasta": ["LCJ"]}  # type: ignore[dict-item]

    # Act / assert.
    assert not search_matches(request, exp)


def test_search_matches_false_when_unexpected_attribute_is_present() -> None:
    # Arrange: an unmodeled non-empty attribute must not be silently accepted.
    exp = expected()
    request = search_request(exp)
    request["Atrybuty"] = {**request["Atrybuty"], "JakasInnaCecha": ["X"]}  # type: ignore[dict-item]

    # Act / assert.
    assert not search_matches(request, exp)


def test_matched_batches_pairs_search_and_cards() -> None:
    # Arrange.
    exp = expected()
    search = ListingExchange(
        1, "/api/wyszukiwarka/v5.0/wyszukaj", search_request(exp), search_response([ROW])
    )
    cards = ListingExchange(
        2, "/api/bloczki/v5.0/pobierz-bloczki", cards_request(exp, [ROW]), [CARD]
    )

    # Act.
    pairs = matched_batches([search, cards], exp)

    # Assert.
    assert pairs == [(search, cards)]


def test_matched_batches_ignores_superseded_search_page() -> None:
    # Arrange: a stale page whose own snapshot would conflict with the visible card,
    # superseded by a fresh, correct re-request of the same page before its cards batch.
    exp = expected()
    stale_row = {**ROW, "Cena": 1400}
    stale_search = ListingExchange(
        1, "/api/wyszukiwarka/v5.0/wyszukaj", search_request(exp), search_response([stale_row])
    )
    stale_cards = ListingExchange(
        2, "/api/bloczki/v5.0/pobierz-bloczki", cards_request(exp, [stale_row]), [CARD]
    )
    fresh_search = ListingExchange(
        3, "/api/wyszukiwarka/v5.0/wyszukaj", search_request(exp), search_response([ROW])
    )
    fresh_cards = ListingExchange(
        4, "/api/bloczki/v5.0/pobierz-bloczki", cards_request(exp, [ROW]), [CARD]
    )

    # Act: if the stale pair were evaluated instead of skipped, its mismatched price
    # would raise rather than silently succeed.
    evidence = evidence_for(OFFER, [stale_search, stale_cards, fresh_search, fresh_cards], exp)

    # Assert: only the fresh, non-superseded pair is used.
    assert evidence is not None and evidence.opaque_key == "OPAQUE-1"
    assert evidence.price == Decimal("1551")


def test_evidence_for_returns_full_option_lists_not_combinations() -> None:
    # Arrange.
    exp = expected()
    search = ListingExchange(
        1, "/api/wyszukiwarka/v5.0/wyszukaj", search_request(exp), search_response([ROW])
    )
    cards = ListingExchange(
        2, "/api/bloczki/v5.0/pobierz-bloczki", cards_request(exp, [ROW]), [CARD]
    )

    # Act.
    evidence = evidence_for(OFFER, [search, cards], exp)

    # Assert: raw option lists, never a fabricated airport x meal combination.
    assert evidence == ListingEvidence(
        "KEY-1",
        "OPAQUE-1",
        "https://r.pl/turcja-riwiera-wczasy/gardenia-hotel"
        "?unikalnyKluczOferty=OPAQUE-1&liczbaPokoi=1&czyCenaZaWszystkich=0"
        "&wiek=1990-01-01&wiek=1992-06-15",
        BIRTHS,
        date(2026, 12, 5),
        8,
        7,
        Decimal("1551"),
        ("KTW", "WAW"),
        ("HB", "AI"),
    )


def test_evidence_for_none_without_a_matching_offer_url() -> None:
    # Arrange: a card for an unrelated product.
    exp = expected()
    basic = dict(cast("dict[str, object]", CARD["BazoweInformacje"]))
    basic["OfertaUrl"] = (
        "/turcja-riwiera-wczasy/other-hotel"
        "?unikalnyKluczOferty=OPAQUE-1&liczbaPokoi=1&czyCenaZaWszystkich=0"
        "&wiek=1990-01-01&wiek=1992-06-15"
    )
    other = dict(CARD)
    other["BazoweInformacje"] = basic
    search = ListingExchange(
        1, "/api/wyszukiwarka/v5.0/wyszukaj", search_request(exp), search_response([ROW])
    )
    cards = ListingExchange(
        2, "/api/bloczki/v5.0/pobierz-bloczki", cards_request(exp, [ROW]), [other]
    )

    # Act / assert.
    assert evidence_for(OFFER, [search, cards], exp) is None


def test_evidence_for_raises_on_conflicting_listing_data() -> None:
    # Arrange: the search row and the visible card disagree on price.
    exp = expected()
    mismatched_row = {**ROW, "Cena": 1400}
    search = ListingExchange(
        1, "/api/wyszukiwarka/v5.0/wyszukaj", search_request(exp), search_response([mismatched_row])
    )
    cards = ListingExchange(
        2, "/api/bloczki/v5.0/pobierz-bloczki", cards_request(exp, [mismatched_row]), [CARD]
    )

    # Act / assert: never guess which price is correct.
    with pytest.raises(RainbowDetailError):
        evidence_for(OFFER, [search, cards], exp)


def test_evidence_for_raises_on_ambiguous_candidates() -> None:
    # Arrange: two independent, non-superseded pages both yield a valid candidate.
    exp = expected()
    first = ListingExchange(
        1, "/api/wyszukiwarka/v5.0/wyszukaj", search_request(exp, strona=1), search_response([ROW])
    )
    first_cards = ListingExchange(
        2, "/api/bloczki/v5.0/pobierz-bloczki", cards_request(exp, [ROW]), [CARD]
    )
    second = ListingExchange(
        3, "/api/wyszukiwarka/v5.0/wyszukaj", search_request(exp, strona=2), search_response([ROW])
    )
    second_cards = ListingExchange(
        4, "/api/bloczki/v5.0/pobierz-bloczki", cards_request(exp, [ROW]), [CARD]
    )

    # Act / assert.
    with pytest.raises(RainbowDetailError, match="Ambiguous"):
        evidence_for(OFFER, [first, first_cards, second, second_cards], exp)
