"""Offline selected-configuration evidence, independent of Offer and acquisition."""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, cast
from urllib.parse import parse_qs

from .rainbow_nuxt import NuxtTable, RainbowDetailError, mapping, require, sequence


@dataclass(frozen=True)
class RainbowFlight:
    departure_airport: str
    arrival_airport: str
    departure_date: date
    arrival_date: date
    departure_time: datetime | None
    arrival_time: datetime | None


@dataclass(frozen=True)
class RainbowRoom:
    type_id: int
    configuration_id: int
    name: str
    count: int


@dataclass(frozen=True)
class RainbowSelectedVariant:
    product_key: str
    opaque_key: str
    outbound: RainbowFlight
    inbound: RainbowFlight
    board: str
    board_source: str
    room: RainbowRoom
    adults: int
    children: int
    departure_date: date
    return_date: date
    days: int
    nights: int
    price_per_person: Decimal
    total_price: Decimal
    configuration_verified: Literal[True] = field(default=True, init=False)
    price_is_complete: Literal[False] = field(default=False, init=False)
    price_verification_reason: str = field(
        default="Selected calculator amounts only; mandatory costs are unverified", init=False
    )
    provenance: str = field(default="__NUXT_DATA__:pinia.kartaHotelu", init=False)


def _text(value: object) -> str:
    require(isinstance(value, str) and bool(value.strip()), "Missing or invalid detail string")
    return cast(str, value)


def _int(value: object) -> int:
    require(type(value) is int and value >= 0, "Invalid detail integer")
    return cast(int, value)


def _money(value: object) -> Decimal:
    require(type(value) in {int, Decimal}, "Invalid detail price type")
    amount = Decimal(cast(int | Decimal, value))
    require(amount.is_finite() and 0 < amount < Decimal("1e10"), "Invalid detail price")
    require(amount == amount.quantize(Decimal("0.01")), "Unsupported price precision")
    return amount


def _date(value: object) -> date:
    text = _text(value)
    require(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) is not None, "Invalid detail date")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise RainbowDetailError("Invalid detail date") from exc


def _one(value: object) -> dict[str, object]:
    items = sequence(value)
    require(len(items) == 1, "Unsupported or ambiguous multi-record selection")
    return mapping(items[0])


def _active(value: object) -> dict[str, object]:
    records = [mapping(item) for item in sequence(value)]
    require(all(type(r.get("CzyAktywna")) is bool for r in records), "Missing active flag")
    return _one([r for r in records if r["CzyAktywna"] is True])


def _iata(value: object) -> str:
    code = _text(value)
    require(re.fullmatch("[A-Z]{3}", code) is not None, "Invalid airport code")
    return code


def _flight(value: object) -> RainbowFlight:
    record = mapping(value)
    require(record.get("CzySamolotem") is True, "Unsupported non-flight route")
    visible = record.get("CzyWyswietlacGodzine")
    require(type(visible) is bool, "Missing flight time visibility")
    times: list[datetime] = []
    for key in ("TerminWyjazdu", "TerminDojazdu"):
        text = _text(record.get(key))
        require(
            re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", text) is not None,
            "Unsupported flight timestamp",
        )
        try:
            times.append(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError as exc:
            raise RainbowDetailError("Invalid flight timestamp") from exc
    require(times[1].date() >= times[0].date(), "Conflicting flight dates")
    if visible:
        require(times[1] >= times[0], "Conflicting flight times")
    return RainbowFlight(
        _iata(record.get("IataWyjazdu")),
        _iata(record.get("IataDojazdu")),
        times[0].date(),
        times[1].date(),
        times[0] if visible else None,
        times[1] if visible else None,
    )


def _params(value: object) -> dict[str, list[str]]:
    text = _text(value)
    require(text.startswith("?") and len(text) < 20_000, "Invalid booking parameters")
    try:
        return parse_qs(text[1:], strict_parsing=True, keep_blank_values=True, max_num_fields=100)
    except ValueError as exc:
        raise RainbowDetailError("Invalid booking parameters") from exc


def _param(params: dict[str, list[str]], name: str) -> str:
    values = params.get(name, [])
    require(len(values) == 1 and bool(values[0]), f"Missing or ambiguous parameter: {name}")
    return values[0]


def _equal(actual: object, expected: object, name: str) -> None:
    require(type(actual) is type(expected) and actual == expected, f"Conflicting {name}")


def _party_room(
    hotel: dict[str, object],
    selected: dict[str, object],
    block: dict[str, object],
    params: dict[str, list[str]],
    departure: date,
) -> RainbowRoom:
    _equal(_param(params, "liczbaDoroslych"), "2", "adult count")
    _equal(_param(params, "liczbaDzieci"), "0", "child count")
    _equal(hotel["wybranaLiczbaPokoi"], 1, "room count")
    _equal(hotel["wybraneDatyDzieci"], [], "children")
    adults = sequence(hotel["wybraneDatyDorosli"])
    people = sequence(selected.get("OsobyHTP"))
    require(len(adults) == len(people) == 2, "Unsupported party; expected two adults")
    assignments: list[dict[str, object]] = []
    for index, item in enumerate(people):
        person = mapping(item)
        birth = _date(person.get("DataUrodzenia"))
        _equal(person.get("DataUrodzenia"), adults[index], "adult birth date")
        age = (
            departure.year
            - birth.year
            - ((departure.month, departure.day) < (birth.month, birth.day))
        )
        require(18 <= age <= 120, "Unsupported adult age")
        require("DataUrodzeniaInfanta" in person, "Missing infant evidence")
        require(person["DataUrodzeniaInfanta"] is None, "Unsupported infant")
        room = _one(person.get("Pokoje"))
        _equal(room.get("NumerPokoju"), 0, "participant room")
        _equal(room.get("NumerOsoby"), index, "participant index")
        assignments.append(room)
    type_id = _int(assignments[0].get("TypPokojuId"))
    config_id = _int(assignments[0].get("KonfiguracjaId"))
    require(type_id > 0 and config_id > 0, "Invalid room identity")
    for assignment in assignments:
        _equal(assignment.get("TypPokojuId"), type_id, "room type")
        _equal(assignment.get("KonfiguracjaId"), config_id, "room configuration")
    masks = sequence(block.get("WybraneMaski"))
    _equal(hotel["wybranePokoje"], masks, "selected room masks")
    require(len(masks) == 1 and type(masks[0]) is int, "Unsupported room masks")
    room_type = _one(
        [
            item
            for item in sequence(block.get("TypyPokoi"))
            if mapping(item).get("MaskaTypuPokoju") == masks[0]
        ]
    )
    _equal(room_type.get("TypPokojuId"), type_id, "selected room type")
    _equal(_param(params, "pokojParams"), f"1_1_{config_id}_{type_id}_2_2", "room parameters")
    _equal(_param(params, "pokojParamsV2"), f"1_1_{type_id}_{config_id}_0_2", "room parameters V2")
    expected_people = [f"{i}_{_text(birth)}_1_1_0" for i, birth in enumerate(adults)]
    _equal(params.get("uczestnikParamsV2"), expected_people, "participant parameters")
    names = mapping(mapping(hotel["offerInfo"]).get("NazwyPokoi"))
    return RainbowRoom(type_id, config_id, _text(names.get(str(type_id))), 1)


def _selected_board(block: dict[str, object], params: dict[str, list[str]]) -> tuple[str, str]:
    """Return (canonical board, Rainbow board code) for the two observed board options."""
    board = _active(block.get("Wyzywienia"))
    board_code = _text(board.get("Wartosc"))
    boards = {"2-posilki": ("HB", "2 posiłki"), "all-inclusive": ("AI", "All inclusive")}
    require(board_code in boards, "Unsupported selected board")
    normalized_board, label = boards[board_code]
    _equal(board.get("Nazwa"), label, "board label")
    _equal(_param(params, "wybraneWyzywienie"), board_code, "booking board")
    return normalized_board, board_code


def _selected_prices(
    selected: dict[str, object], term: dict[str, object]
) -> tuple[Decimal, Decimal]:
    """Return (per-person, total) once calculator, date and per-participant prices agree."""
    price, total = _money(selected.get("CenaAvg")), _money(selected.get("CenaSum"))
    require(price * 2 == total, "Conflicting calculator prices; rounded averages unsupported")
    _equal(_money(term.get("CenaAvg")), price, "selected date price")
    _equal(_money(term.get("CenaSum")), total, "selected date total")
    participant_prices = sequence(selected.get("CenyZaOsoby"))
    require(len(participant_prices) == 2, "Conflicting participant prices")
    amounts: list[Decimal] = []
    for index, item in enumerate(participant_prices):
        quote = mapping(item)
        _equal(quote.get("NrPokoju"), 0, "price room")
        _equal(quote.get("NrOsoby"), index, "price participant")
        amounts.append(_money(quote.get("Cena")))
    require(sum(amounts, Decimal(0)) == total, "Conflicting participant total")
    return price, total


def parse_selected_variant(
    html: str,
    *,
    expected_product_key: str,
    expected_opaque_key: str,
    expected_birth_dates: tuple[str, ...] | None = None,
) -> RainbowSelectedVariant:
    """Validate the observed single-stay, two-adult, one-room configuration.

    This confirms agreement within a saved document, not current availability,
    mandatory costs, stable booking identity or alternative-choice prices.
    """
    _text(expected_product_key)
    _text(expected_opaque_key)
    table = NuxtTable(html)
    hotel = {
        key: table.read("pinia", "kartaHotelu", key)
        for key in (
            "kalkulator",
            "flightInfo",
            "wybranaLiczbaPokoi",
            "wybraneDatyDorosli",
            "wybraneDatyDzieci",
            "wybranePokoje",
        )
    }
    hotel["offerInfo"] = {
        key: table.read("pinia", "kartaHotelu", "offerInfo", key)
        for key in ("NazwyPokoi", "HotelId", "Id")
    }
    if expected_birth_dates is not None:
        _equal(
            hotel["wybraneDatyDorosli"], list(expected_birth_dates), "listing/detail adult dates"
        )
    calc = mapping(hotel["kalkulator"])
    require(calc.get("CzyIstniejeWycieczka") is True, "Selected trip does not exist")
    keys = mapping(calc.get("KluczOferty"))
    _equal(keys.get("KluczProduktoHotelWC"), expected_product_key, "product key")
    components = [
        _int(keys.get(name)) for name in ("HotelId", "ProduktId", "ImprezaId", "HotelWCId")
    ]
    require(all(value > 0 for value in components), "Invalid product identity")
    _equal(
        f"{components[0]}_{components[1]}:{components[2]}:{components[3]}",
        expected_product_key,
        "product identity components",
    )
    selected = mapping(calc.get("Wybrana"))
    _equal(selected.get("UnikalnyKluczOferty"), expected_opaque_key, "selected opaque key")
    params = _params(selected.get("RezerwujParametry"))
    _equal(_param(params, "kodWycieczki"), _text(keys.get("KodProduktu")), "product code")
    hotel_id = _int(keys.get("HotelId"))
    _equal(_param(params, "hotelId"), str(hotel_id), "hotel ID")
    info = mapping(hotel["offerInfo"])
    _equal(info.get("HotelId"), hotel_id, "room-name hotel ID")
    _equal(info.get("Id"), keys.get("HotelWCId"), "room-name offer ID")
    departure = _date(_param(params, "dataStart"))
    duration = _active(calc.get("Terminy"))
    term = _active(duration.get("Terminy"))
    _equal(term.get("UnikalnyKluczOferty"), expected_opaque_key, "selected date key")
    days, nights = _int(duration.get("LiczbaDniSum")), _int(duration.get("LiczbaNocySum"))
    require(nights > 0 and days == nights + 1, "Unsupported stay duration")
    _equal(term.get("LiczbaDniSum"), days, "selected duration")
    _equal(duration.get("LiczbyDniBlokow"), [days], "stay days")
    _equal(duration.get("LiczbaNocyBlokow"), [nights], "stay nights")
    _equal(term.get("LiczbyDniBlokow"), [days], "selected stay days")
    _equal(_param(params, "liczbaNocy1"), str(nights), "booking nights")
    dates = sequence(term.get("DatyBlokow"))
    require(len(dates) == 2, "Unsupported multiple stays")
    _equal(_date(dates[0]), departure, "departure date")
    end = _date(dates[1])
    require((end - departure).days == nights, "Conflicting stay dates")
    _equal(_param(params, "hotelParams"), f"1_{hotel_id}_{nights}", "hotel parameters")
    _equal(
        _param(params, "hotelParamsV2"), f"1_{hotel_id}_{departure}_{nights}", "hotel parameters V2"
    )
    block = _one(calc.get("Bloki"))
    normalized_board, board_code = _selected_board(block, params)
    room = _party_room(hotel, selected, block, params, departure)
    connection = _active(calc.get("Polaczenia"))
    _equal(connection.get("UnikalnyKluczOferty"), expected_opaque_key, "connection key")
    stop = _active(calc.get("PrzystankiWyjazdowe"))
    _equal(stop.get("UnikalnyKluczOferty"), expected_opaque_key, "departure stop key")
    for key in ("Id", "Iata", "TrasaWCId", "TypTransportu"):
        _equal(stop.get(key), mapping(connection.get("Wyjazd")).get(key), "departure stop")
    flight = mapping(hotel["flightInfo"])
    _equal(flight.get("UnikalnyKluczOferty"), expected_opaque_key, "flight key")
    require(flight.get("CzyAktywna") is True, "Inactive flight selection")
    outbound, inbound = _flight(flight.get("TrasaWyjazdowa")), _flight(flight.get("TrasaPowrotna"))
    for name, param, airport in (
        ("Wyjazd", "przystanekWyjazdowyId", outbound.departure_airport),
        ("Powrot", "przystanekPowrotnyId", inbound.arrival_airport),
    ):
        route = mapping(connection.get(name))
        _equal(route.get("TypTransportu"), "AIR", "transport")
        _equal(_param(params, param), str(_int(route.get("Id"))), "booking stop")
        _equal(_iata(route.get("Iata")), airport, "flight airport")
    _equal(outbound.arrival_airport, inbound.departure_airport, "destination airport")
    _equal(outbound.departure_date, departure, "outbound date")
    _equal(outbound.arrival_date, departure, "arrival date")
    _equal(inbound.departure_date, end, "inbound date")
    _equal(inbound.arrival_date, end, "return date")
    price, total = _selected_prices(selected, term)
    return RainbowSelectedVariant(
        expected_product_key,
        expected_opaque_key,
        outbound,
        inbound,
        normalized_board,
        board_code,
        room,
        2,
        0,
        departure,
        end,
        days,
        nights,
        price,
        total,
    )
