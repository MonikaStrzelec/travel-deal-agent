"""Join captured listing evidence without inventing variant combinations or URLs."""

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, DecimalException
from typing import cast
from urllib.parse import parse_qs, urljoin, urlsplit

from ..boards import normalize_board
from ..models import Offer
from .rainbow_nuxt import RainbowDetailError, mapping, require, sequence

MAX_JSON_BYTES = 2_000_000
SEARCH_PATH = "/api/wyszukiwarka/v5.0/wyszukaj"
CARDS_PATH = "/api/bloczki/v5.0/pobierz-bloczki"


@dataclass(frozen=True)
class ListingExchange:
    order: int
    path: str
    request: dict[str, object]
    response: object


@dataclass(frozen=True)
class ListingEvidence:
    product_key: str
    opaque_key: str
    url: str
    birth_dates: tuple[str, ...]
    departure_date: date
    days: int
    nights: int
    price: Decimal
    airports: tuple[str, ...]
    boards: tuple[str, ...]


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        require(key not in result, "Duplicate listing JSON field")
        result[key] = value
    return result


def _nonfinite(value: str) -> object:
    raise RainbowDetailError("Nonfinite listing JSON number")


def listing_json(body: bytes) -> object:
    require(len(body) <= MAX_JSON_BYTES, "Listing response exceeds size limit")
    try:
        return cast(
            object,
            json.loads(
                body,
                parse_float=Decimal,
                parse_constant=_nonfinite,
                object_pairs_hook=_object,
            ),
        )
    except (ValueError, RecursionError, DecimalException) as exc:
        raise RainbowDetailError("Invalid listing JSON") from exc


def _strings(value: object) -> tuple[str, ...]:
    values = sequence(value)
    require(all(isinstance(v, str) and v for v in values), "Invalid listing options")
    return tuple(cast(list[str], values))


def _text(value: object) -> str:
    require(isinstance(value, str) and bool(value), "Missing listing string")
    return cast(str, value)


def _integer(value: object) -> int:
    require(type(value) is int and value > 0, "Invalid listing integer")
    return cast(int, value)


def _price(value: object) -> Decimal:
    require(type(value) in {int, Decimal}, "Invalid listing price")
    result = Decimal(cast(int | Decimal, value))
    require(result.is_finite() and 0 < result < Decimal("1e10"), "Invalid listing price")
    return result


def _date(value: object) -> date:
    text = _text(value)
    require(
        re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text) is not None,
        "Unsupported listing timestamp",
    )
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise RainbowDetailError("Invalid listing date") from exc


def product_url(value: str) -> str:
    require(not any(ord(c) <= 32 for c in value) and "\\" not in value, "Unsafe detail URL")
    url = urljoin("https://r.pl", value)
    parts = urlsplit(url)
    require(
        parts.scheme == "https" and parts.netloc == "r.pl" and not parts.fragment,
        "Unexpected detail host",
    )
    require(
        re.fullmatch(r"/[a-z0-9-]+/[a-z0-9-]+", parts.path) is not None
        and parts.path.split("/")[1] not in {"api", "koszyk", "rezerwacja", "szukaj"},
        "Unexpected detail path",
    )
    return url


def search_matches(request: dict[str, object], expected: dict[str, list[str]]) -> bool:
    """Use final verified UI values, not callback timing or response positions."""
    try:
        attributes = mapping(request.get("Atrybuty"))
        required = {
            "Miasta": expected["wybraneSkad"],
            "Wyzywienia": expected["wyzywienia"],
            "StandardHotelu": expected["standardHotelu"],
            "DlugoscPobytu": expected["dlugoscPobytu"],
            "Cena": ["avg", "*-" + expected["cena.do"][0]],
            "OcenaKlientow": expected.get("ocenaKlientow", ["*-*"]),
            "OdlegloscLotnisko": ["*-*"],
            "TypTransportu": ["AIR", "dreamliner"],
            "Lokalizacje_HoteloProdukt": [],
        }
        return (
            request.get("Sortowanie") == "cena-asc"
            and expected.get("sortowanie") == ["cena-asc"]
            and request.get("DatyUrodzenia") == expected["dorosli"]
            and len(expected["dorosli"]) == 2
            and type(request.get("LiczbaPokoi")) is int
            and request["LiczbaPokoi"] == 1
            and request.get("CzyWeekendowka") is False
            and request.get("PowrotNaInneLotnisko") is False
            and not request.get("CzyPotwierdzone")
            and all(sorted(_strings(attributes.get(k))) == sorted(v) for k, v in required.items())
            and not any(v for k, v in attributes.items() if k not in required)
        )
    except (RainbowDetailError, KeyError, IndexError, TypeError):
        return False


def matched_batches(
    exchanges: list[ListingExchange],
    expected: dict[str, list[str]],
) -> list[tuple[ListingExchange, ListingExchange]]:
    """Join requests to responses by complete search records and the same party."""
    searches = [
        e for e in exchanges if e.path == SEARCH_PATH and search_matches(e.request, expected)
    ]
    pairs: list[tuple[ListingExchange, ListingExchange]] = []
    for search in searches:
        result = mapping(search.response)
        rows = sequence(result.get("Wynik"))
        if result.get("CzyCenaZaOsobe") is not True:
            continue
        for cards in exchanges:
            if (
                cards.path != CARDS_PATH
                or cards.order <= search.order
                or cards.request.get("DatyUrodzenia") != search.request.get("DatyUrodzenia")
                or cards.request.get("LiczbaPokoi") != 1
                or cards.request.get("CzyCenaZaOsobe") is not True
            ):
                continue
            params = sequence(cards.request.get("Parametry"))
            # Order is not the join key; every requested row must occur exactly once.
            if params and all(params.count(row) == rows.count(row) == 1 for row in params):
                pairs.append((search, cards))
    return pairs


def evidence_for(
    offer: Offer,
    exchanges: list[ListingExchange],
    expected: dict[str, list[str]],
) -> ListingEvidence | None:
    pairs = matched_batches(exchanges, expected)
    candidates: list[ListingEvidence] = []
    for search, cards in pairs:
        # A later request for this page supersedes an earlier snapshot, even if capture failed.
        if any(
            e.path == SEARCH_PATH
            and e.order > search.order
            and e.request.get("Strona", 1) == search.request.get("Strona", 1)
            and search_matches(e.request, expected)
            for e in exchanges
        ):
            continue
        for raw in sequence(cards.response):
            card = mapping(raw)
            basic = mapping(card.get("BazoweInformacje"))
            url = product_url(_text(basic.get("OfertaUrl")))
            if urlsplit(url).path != urlsplit(offer.url or "").path:
                continue
            key, opaque = _text(card.get("Klucz")), _text(card.get("UnikalnyKluczOferty"))
            rows = [
                mapping(row)
                for row in sequence(cards.request.get("Parametry"))
                if mapping(row).get("Id") == key
            ]
            require(len(rows) == 1, "Missing or ambiguous search key")
            row = rows[0]
            require(row.get("UnikalnyKluczOferty") == opaque, "Conflicting listing opaque key")
            amount = mapping(card.get("Cena"))
            require(amount.get("CzyCenaZaOsobe") is True, "Listing price is not per person")
            price = _price(amount.get("Cena"))
            days, nights = _integer(basic.get("LiczbaDni")), _integer(basic.get("LiczbaNocy"))
            departure = _date(card.get("TerminWyjazdu"))
            require(
                price == _price(row.get("Cena")) == offer.price_per_person
                and departure == _date(row.get("TerminWyjazdu")) == offer.departure_date
                and days == row.get("LiczbaDni") == offer.number_of_days
                and basic.get("GwiazdkiHotelu") == offer.hotel_stars
                and basic.get("NazwaHoteluWWW") == offer.hotel_name,
                "Listing JSON and visible card disagree",
            )
            births = _strings(cards.request.get("DatyUrodzenia"))
            require(
                basic.get("DatyUrodzenia") == list(births) and basic.get("LiczbaPokoi") == 1,
                "Conflicting listing party",
            )
            query = parse_qs(urlsplit(url).query, keep_blank_values=True)
            require(
                query
                == {
                    "unikalnyKluczOferty": [opaque],
                    "liczbaPokoi": ["1"],
                    "czyCenaZaWszystkich": ["0"],
                    "wiek": list(births),
                },
                "Conflicting detail link parameters",
            )
            airports = tuple(
                _text(mapping(p).get("Iata")) for p in sequence(card.get("Przystanki"))
            )
            boards = tuple(
                normalize_board("rainbow", _text(mapping(b).get("Nazwa")))
                for b in sequence(card.get("Wyzywienia"))
            )
            require(
                bool(airports) and bool(boards) and all(b is not None for b in boards),
                "Unknown listing airport/meal options",
            )
            candidates.append(
                ListingEvidence(
                    key,
                    opaque,
                    url,
                    births,
                    departure,
                    days,
                    nights,
                    price,
                    airports,
                    cast(tuple[str, ...], boards),
                )
            )
    require(len(candidates) <= 1, "Ambiguous listing evidence for visible product")
    return candidates[0] if candidates else None
