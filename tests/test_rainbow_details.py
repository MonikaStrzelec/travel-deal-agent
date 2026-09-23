"""Offline detail contract tests; no ignored artifacts or browser dependencies."""

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from travel_deal_agent.providers.rainbow_details import (
    RainbowSelectedVariant,
    parse_selected_variant,
)
from travel_deal_agent.providers.rainbow_nuxt import MAX_HTML_BYTES, NuxtTable, RainbowDetailError

FIXTURE = Path(__file__).parent / "fixtures" / "rainbow" / "detail.html"
PRODUCT = "6466_12682:249522:10474247"
OPAQUE = "AkIZAooxA7LOAwEBAwfTnwEBAjE5ASMBAgEBAQEBAQABAgNm/REBAgOX/REBAgECAQc="


def parse(html: str, product: str = PRODUCT, opaque: str = OPAQUE) -> RainbowSelectedVariant:
    return parse_selected_variant(html, expected_product_key=product, expected_opaque_key=opaque)


def document(table: list[object]) -> str:
    return '<script id="__NUXT_DATA__">' + json.dumps(table) + "</script>"


def fixture_table() -> list[object]:
    html = FIXTURE.read_text(encoding="utf-8")
    return cast(list[object], json.loads(html.split('application/json">')[1].split("</script>")[0]))


def decoded_fixture() -> dict[str, object]:
    """Independent fixture reader, not the production decoder under test."""
    table = fixture_table()

    def read(index: int) -> object:
        value = table[index]
        if isinstance(value, dict):
            return {k: read(v) for k, v in value.items()}
        if isinstance(value, list):
            if value and isinstance(value[0], str):
                result = read(value[1])
                return cast(str, result)[:10] if value[0] == "LocalDate" else result
            return [read(i) for i in value]
        return value

    return cast(dict[str, object], read(0))


def encode(value: object) -> str:
    """Independent plain reference-table writer, without scalar deduplication."""
    table: list[object] = []

    def add(item: object) -> int:
        index = len(table)
        table.append(None)
        if isinstance(item, dict):
            table[index] = {k: add(v) for k, v in item.items()}
        elif isinstance(item, list):
            table[index] = [add(v) for v in item]
        else:
            table[index] = item
        return index

    add(value)
    return document(table)


def node(root: object, *path: str | int) -> object:
    for part in path:
        root = (
            cast(list[object], root)[part]
            if isinstance(part, int)
            else cast(dict[str, object], root)[part]
        )
    return root


def change(root: object, path: tuple[str | int, ...], value: object) -> None:
    parent = node(root, *path[:-1])
    last = path[-1]
    if isinstance(last, int):
        cast(list[object], parent)[last] = value
    else:
        cast(dict[str, object], parent)[last] = value


HOTEL = ("pinia", "kartaHotelu")
CALC = (*HOTEL, "kalkulator")
SELECTED = (*CALC, "Wybrana")
BLOCK = (*CALC, "Bloki", 0)
FLIGHT = (*HOTEL, "flightInfo")


def test_saved_gardenia_configuration() -> None:
    # Arrange
    html = FIXTURE.read_text(encoding="utf-8")
    # Act
    result = parse(html)
    # Assert
    assert result.product_key == PRODUCT
    assert result.opaque_key == OPAQUE
    assert (result.outbound.departure_airport, result.inbound.arrival_airport) == ("KTW", "KTW")
    assert result.outbound.arrival_airport == result.inbound.departure_airport == "AYT"
    assert (result.departure_date, result.return_date) == (date(2026, 12, 5), date(2026, 12, 12))
    assert (result.days, result.nights, result.adults, result.children) == (8, 7, 2, 0)
    assert (result.board, result.board_source) == ("HB", "2-posilki")
    assert (result.room.type_id, result.room.configuration_id, result.room.count) == (14641, 35, 1)
    assert result.room.name == "Pokój economy"
    assert result.price_per_person == Decimal("1551")
    assert result.total_price == Decimal("3102")
    assert isinstance(result.price_per_person, Decimal)
    assert result.configuration_verified is True
    assert result.price_is_complete is False
    assert "mandatory costs" in result.price_verification_reason
    assert result.outbound.departure_time is result.outbound.arrival_time is None
    assert result.inbound.departure_time is result.inbound.arrival_time is None


def test_reference_indices_are_not_hardcoded() -> None:
    # Arrange: reverse every index except the format-defined root index zero.
    table = fixture_table()
    indices = {i: len(table) - i if i else 0 for i in range(len(table))}
    shifted: list[object] = [None] * len(table)
    for i, value in enumerate(table):
        if isinstance(value, dict):
            moved: object = {k: indices[v] for k, v in value.items()}
        elif isinstance(value, list):
            moved = (
                [value[0], indices[value[1]]]
                if value and isinstance(value[0], str)
                else [indices[v] for v in value]
            )
        else:
            moved = value
        shifted[indices[i]] = moved
    # Act / Assert
    assert parse(document(shifted)) == parse(FIXTURE.read_text(encoding="utf-8"))
    assert parse(encode(decoded_fixture())) == parse(document(shifted))


@pytest.mark.parametrize("product,opaque", [("other", OPAQUE), (PRODUCT, "other"), ("", OPAQUE)])
def test_expected_identity_is_required(product: str, opaque: str) -> None:
    # Arrange
    html = FIXTURE.read_text(encoding="utf-8")
    # Act / Assert
    with pytest.raises(RainbowDetailError):
        parse(html, product, opaque)


@pytest.mark.parametrize(
    "path,value",
    [
        ((*SELECTED, "UnikalnyKluczOferty"), "other"),
        ((*CALC, "CzyIstniejeWycieczka"), False),
        ((*CALC, "KluczOferty", "ProduktId"), 123),
        ((*CALC, "Polaczenia", 0, "UnikalnyKluczOferty"), "other"),
        ((*CALC, "Polaczenia", 1, "CzyAktywna"), True),
        ((*CALC, "Polaczenia", 0, "CzyAktywna"), False),
        ((*CALC, "Polaczenia", 0, "CzyAktywna"), 1),
        ((*CALC, "Polaczenia", 0, "Wyjazd", "Id"), 123),
        ((*CALC, "PrzystankiWyjazdowe", 0, "Iata"), "WAW"),
        ((*CALC, "Terminy", 0, "Terminy", 0, "UnikalnyKluczOferty"), "other"),
        ((*CALC, "Terminy", 0, "LiczbaNocySum"), 6),
        ((*CALC, "Terminy", 0, "Terminy", 0, "DatyBlokow", 1), "2026-12-13"),
        ((*BLOCK, "Wyzywienia", 0, "Wartosc"), "all-inclusive"),
        ((*BLOCK, "Wyzywienia", 0, "Nazwa"), "All inclusive"),
        ((*BLOCK, "TypyPokoi", 0, "TypPokojuId"), 123),
        ((*HOTEL, "wybranePokoje"), [2]),
        ((*HOTEL, "wybranaLiczbaPokoi"), 2),
        ((*HOTEL, "wybraneDatyDzieci"), ["2020-01-01"]),
        ((*HOTEL, "wybraneDatyDorosli"), ["1990-01-01"]),
        ((*HOTEL, "offerInfo", "HotelId"), 123),
        ((*HOTEL, "offerInfo", "Id"), 123),
        ((*SELECTED, "OsobyHTP", 1, "DataUrodzenia"), "2020-01-01"),
        ((*SELECTED, "OsobyHTP", 1, "DataUrodzeniaInfanta"), "2026-01-01"),
        ((*SELECTED, "OsobyHTP", 1, "Pokoje", 0, "KonfiguracjaId"), 99),
        ((*SELECTED, "CenaSum"), 3103),
        ((*SELECTED, "CenaAvg"), True),
        ((*SELECTED, "CenaAvg"), "1551"),
        ((*SELECTED, "CenaAvg"), -1),
        ((*SELECTED, "CenaAvg"), 1e30),
        ((*SELECTED, "CenyZaOsoby", 1, "Cena"), 1552),
        ((*SELECTED, "CenyZaOsoby", 1, "NrOsoby"), 0),
        ((*CALC, "Terminy", 0, "Terminy", 0, "CenaSum"), 3103),
        ((*FLIGHT, "UnikalnyKluczOferty"), "other"),
        ((*FLIGHT, "TrasaWyjazdowa", "IataWyjazdu"), "WAW"),
        ((*FLIGHT, "TrasaPowrotna", "IataDojazdu"), "WAW"),
        ((*FLIGHT, "TrasaPowrotna", "TerminDojazdu"), "2026-12-13T13:13:00Z"),
        ((*FLIGHT, "TrasaWyjazdowa", "TerminWyjazdu"), "2026-02-30T13:13:00Z"),
        ((*FLIGHT, "TrasaWyjazdowa", "CzyWyswietlacGodzine"), None),
        ((*FLIGHT, "TrasaWyjazdowa", "CzySamolotem"), False),
    ],
)
def test_conflicting_or_unsupported_evidence(path: tuple[str | int, ...], value: object) -> None:
    # Arrange
    root = decoded_fixture()
    change(root, path, value)
    # Act / Assert
    with pytest.raises(RainbowDetailError):
        parse(encode(root))


@pytest.mark.parametrize(
    "old,new",
    [
        ("liczbaDoroslych=2", "liczbaDoroslych=3"),
        ("kodWycieczki=TRA", "kodWycieczki=OTHER"),
        ("liczbaDzieci=0", "liczbaDzieci=1"),
        ("liczbaNocy1=7", "liczbaNocy1=8"),
        ("wybraneWyzywienie=2-posilki", "wybraneWyzywienie=all-inclusive"),
        ("przystanekPowrotnyId=1179031", "przystanekPowrotnyId=1179032"),
        ("hotelId=6466", "hotelId=6466&hotelId=999"),
        ("pokojParamsV2=1_1_14641_35_0_2", "pokojParamsV2=1_1_14641_35_0_3"),
        ("uczestnikParamsV2=1_1990-01-01_1_1_0", "uczestnikParamsV2=0_1990-01-01_1_1_0"),
    ],
)
def test_booking_parameters_must_agree(old: str, new: str) -> None:
    # Arrange
    root = decoded_fixture()
    original = cast(str, node(root, *SELECTED, "RezerwujParametry"))
    change(root, (*SELECTED, "RezerwujParametry"), original.replace(old, new))
    # Act / Assert
    with pytest.raises(RainbowDetailError):
        parse(encode(root))


def test_prices_use_decimal_and_ignore_alternative_surcharges() -> None:
    # Arrange
    root = decoded_fixture()
    for path in (SELECTED, (*CALC, "Terminy", 0, "Terminy", 0)):
        change(root, (*path, "CenaAvg"), 1551.25)
        change(root, (*path, "CenaSum"), 3102.5)
    for index in (0, 1):
        change(root, (*SELECTED, "CenyZaOsoby", index, "Cena"), 1551.25)
    change(root, (*BLOCK, "Wyzywienia", 1, "Doplata"), 999999)
    change(root, (*CALC, "Polaczenia", 1, "Doplata"), 999999)
    # Act
    result = parse(encode(root))
    # Assert
    assert result.price_per_person == Decimal("1551.25")
    assert result.total_price == Decimal("3102.50")
    assert result.board == "HB"
    assert result.outbound.departure_airport == "KTW"
    assert result.price_is_complete is False


def test_explicitly_visible_times_are_preserved() -> None:
    # Arrange
    root = decoded_fixture()
    change(root, (*FLIGHT, "TrasaWyjazdowa", "CzyWyswietlacGodzine"), True)
    change(root, (*FLIGHT, "TrasaWyjazdowa", "TerminWyjazdu"), "2026-12-05T09:00:00Z")
    change(root, (*FLIGHT, "TrasaWyjazdowa", "TerminDojazdu"), "2026-12-05T12:00:00Z")
    # Act
    result = parse(encode(root))
    # Assert
    assert result.outbound.departure_time == datetime(2026, 12, 5, 9, tzinfo=timezone.utc)
    assert result.inbound.departure_time is None


@pytest.mark.parametrize(
    "html",
    [
        "",
        "<script>window.__NUXT_DATA__ = []</script>",
        '<script id="__NUXT_DATA__">[]',
        document([]),
        document([{}]) * 2,
        '<script id="__NUXT_DATA__">{broken}</script>',
        '<script id="__NUXT_DATA__">[{"pinia":1,"pinia":2},null,null]</script>',
        document([float("nan")]),
        pytest.param("x" * (MAX_HTML_BYTES + 1), id="oversized-html"),
    ],
)
def test_invalid_document(html: str) -> None:
    # Arrange / Act / Assert
    with pytest.raises(RainbowDetailError):
        parse(html)


@pytest.mark.parametrize(
    "table",
    [
        [{"pinia": 999}],
        [{"pinia": -1}],
        [{"pinia": True}],
        [{"pinia": 0}],
        [["Ref", 0]],
        [["Unknown", 1], {}],
        [["Ref", 1, 2], {}, {}],
        [{"pinia": 1}, {"kartaHotelu": 2}, {"kalkulator": 3}, {"cycle": 3}],
        [{"pinia": 1}, {"kartaHotelu": 2}, {"kalkulator": 3}, [3]],
        [["Ref", i + 1] for i in range(70)] + [{}],
        [None] * 20_001,
    ],
)
def test_bad_reference_graph_is_bounded(table: list[object]) -> None:
    # Arrange / Act / Assert
    with pytest.raises(RainbowDetailError):
        parse(document(table))


def test_reference_fanout_is_bounded() -> None:
    # Arrange: small table with many edges, requiring no unbounded expansion.
    table: list[object] = [{"value": 1}, [2] * 50_001, 7]
    # Act / Assert
    with pytest.raises(RainbowDetailError, match="budget"):
        NuxtTable(document(table)).read("value")


@pytest.mark.parametrize("field", ["CenaAvg", "CenyZaOsoby", "OsobyHTP", "RezerwujParametry"])
def test_required_evidence_cannot_be_missing(field: str) -> None:
    # Arrange
    root = decoded_fixture()
    del cast(dict[str, object], node(root, *SELECTED))[field]
    # Act / Assert
    with pytest.raises(RainbowDetailError):
        parse(encode(root))


def test_different_product_dates_room_and_prices_are_not_hardcoded() -> None:
    # Arrange: explicitly synthetic variant, not another observed Rainbow offer.
    replacements = {
        "6466": "7101",
        "12682": "7202",
        "249522": "730303",
        "10474247": "74040404",
        "14641": "7505",
        "1551": "1001",
        "3102": "2002",
        "2026-12-05": "2027-01-09",
        "2026-12-12": "2027-01-16",
        "Pokój economy": "Synthetic room",
        OPAQUE: "synthetic-selected-key",
    }

    def replace(value: object) -> object:
        if isinstance(value, dict):
            return {cast(str, replace(k)): replace(v) for k, v in value.items()}
        if isinstance(value, list):
            return [replace(v) for v in value]
        if type(value) is int:
            return int(replacements.get(str(value), str(value)))
        if isinstance(value, str):
            for old, new in replacements.items():
                value = value.replace(old, new)
        return value

    root = replace(decoded_fixture())
    # Act
    result = parse(encode(root), "7101_7202:730303:74040404", "synthetic-selected-key")
    # Assert
    assert result.departure_date == date(2027, 1, 9)
    assert result.return_date == date(2027, 1, 16)
    assert result.room.type_id == 7505
    assert result.room.name == "Synthetic room"
    assert result.price_per_person == Decimal("1001")
    assert result.total_price == Decimal("2002")


@pytest.mark.parametrize(
    "replacement",
    [
        ["LocalDate", -1],
        ["LocalDate", 0],
        ["LocalDate", 1, 2],
    ],
)
def test_invalid_local_date_references(replacement: list[object]) -> None:
    # Arrange
    table = fixture_table()
    index = next(
        i for i, v in enumerate(table) if isinstance(v, list) and v and v[0] == "LocalDate"
    )
    table[index] = replacement
    # Act / Assert
    with pytest.raises(RainbowDetailError):
        parse(document(table))


def test_deep_json_is_rejected_without_recursion_escape() -> None:
    # Arrange
    html = '<script id="__NUXT_DATA__">' + "[" * 2000 + "0" + "]" * 2000 + "</script>"
    # Act / Assert
    with pytest.raises(RainbowDetailError):
        parse(html)


@pytest.mark.parametrize(
    "html",
    [
        '<script id="__NUXT_DATA__">[1e9999999999999999999999999999]</script>',
        "\ud800",
    ],
)
def test_invalid_number_or_encoding_has_explicit_error(html: str) -> None:
    # Arrange / Act / Assert
    with pytest.raises(RainbowDetailError):
        parse(html)
