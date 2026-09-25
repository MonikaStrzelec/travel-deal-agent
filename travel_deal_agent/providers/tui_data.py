"""Offline parsing of TUI's filtered search results.

The source is the body of `.../api/services/tui-search/api/search/offers`, observed
by passively listening to responses after opening a filtered `build_search_path`
URL in a real browser (`tui_browser.capture_search_offers`); this module never
calls that endpoint directly. The equivalent results page fetched over plain HTTP
was confirmed to server-render only a loading skeleton, so no offers are ever
obtained from HTML.

Each offer already represents one specific bookable configuration, not a summary
of several options: there is no nested alternative airport/board array and no
aggregate card.

`offerCode` is a long, structured, human-decodable string (airports, dates, hotel and
room codes); it is used directly as `Offer.offer_id`. Its stability across repricing or
repeated scans has not been independently verified.
"""

import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urljoin, urlsplit

from pydantic import Field, ValidationError

from ..boards import normalize_board
from ..models import Offer
from .boundary import Boundary, mapping

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 4_000_000

# Warszawa-Modlin is listed but disabled on the source site; kept for completeness.
AIRPORTS = {
    "Bydgoszcz": "BZG",
    "Gdańsk": "GDN",
    "Katowice": "KTW",
    "Kraków": "KRK",
    "Łódź": "LCJ",
    "Lublin": "LUZ",
    "Poznań": "POZ",
    "Rzeszów": "RZE",
    "Szczecin": "SZZ",
    "Warszawa-Chopina": "WAW",
    "Warszawa-Modlin": "WMI",
    "Warszawa-Radom": "RDO",
    "Wrocław": "WRO",
}
# Source labels, not business thresholds (same convention already used for
# Rainbow). Unknown labels remain unknown rather than guessed.
COUNTRIES = {
    "Turcja": "TR",
    "Grecja": "GR",
    "Egipt": "EG",
    "Tunezja": "TN",
    "Albania": "AL",
    "Hiszpania": "ES",
    "Bułgaria": "BG",
    "Cypr": "CY",
    "Włochy": "IT",
    "Portugalia": "PT",
    "Malta": "MT",
}
_PARTICIPANTS = re.compile(r"(\d+) Dorosłych \+ (\d+) Dzieci")
_DATE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
_TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
_PRICE = re.compile(r"\d+(?:\.\d{1,2})?")


class Breadcrumb(Boundary):
    label: str
    url: str | None = None


class TuiRawOffer(Boundary):
    """Only the fields this parser uses; everything else is ignored, not dropped."""

    hotelCode: str = Field(min_length=1)
    hotelName: str = Field(min_length=1)
    hotelStandard: float = Field(ge=0, le=5)
    offerCode: str = Field(min_length=1)
    duration: int = Field(ge=1)
    offerUrl: str = Field(min_length=1)
    breadcrumbs: list[Breadcrumb] = Field(default_factory=list)
    discountFullPrice: str
    originalFullPrice: str
    discountPerPersonPrice: str
    originalPerPersonPrice: str
    departureDate: str
    returnDate: str
    departureTime: str
    departureAirport: str
    boardType: str
    boardCode: str
    tripAdvisorRating: float | None = None
    tripAdvisorReviewsNo: int | None = Field(default=None, ge=0)
    participants: str
    currency: str = Field(min_length=1)
    soldOut: bool


def _decimal(value: str, field: str) -> Decimal:
    if not _PRICE.fullmatch(value):
        raise ValueError(f"Unrecognized TUI {field}")
    amount = Decimal(value)
    if amount <= 0:
        raise ValueError(f"Nonpositive TUI {field}")
    return amount


def _date(value: str, field: str) -> date:
    if not _DATE.fullmatch(value):
        raise ValueError(f"Unrecognized TUI {field}")
    try:
        return datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError as exc:
        raise ValueError(f"Invalid TUI {field}") from exc


def normalize_offer(raw: dict[str, object], observed_at: datetime) -> Offer:
    """Normalize one already-specific TUI variant; never split or combine options.

    In search results `duration` equals the number of days between departure and
    return; any disagreement means the record cannot be trusted.
    """
    offer = TuiRawOffer.model_validate(raw)
    if offer.soldOut:
        raise ValueError("TUI offer is marked sold out")
    departure = _date(offer.departureDate, "departure date")
    return_date = _date(offer.returnDate, "return date")
    if return_date <= departure:
        raise ValueError("TUI return date does not follow departure date")
    days = (return_date - departure).days
    if days != offer.duration:
        raise ValueError(
            "TUI nights disagree with departure/return dates (search_xhr: expected days == duration)"
        )
    if not _TIME.fullmatch(offer.departureTime):
        raise ValueError("Unrecognized TUI departure time")

    price = _decimal(offer.discountPerPersonPrice, "per-person price")
    original_price = _decimal(offer.originalPerPersonPrice, "original per-person price")
    full_price = _decimal(offer.discountFullPrice, "full price")

    notes: list[str] = [f"Departure time: {offer.departureTime}"]
    if original_price != price:
        notes.append(f"Original price before discount: {original_price} {offer.currency}/person")
    match = _PARTICIPANTS.fullmatch(offer.participants)
    number_of_people: int | None = None
    total_price: Decimal | None = None
    if match is None:
        notes.append(f"Unrecognized TUI participants text: {offer.participants!r}")
    else:
        adults, children = int(match[1]), int(match[2])
        number_of_people = adults + children
        if adults == 2 and children == 0:
            # discountFullPrice is the authoritative party total; discountPerPersonPrice
            # can be its rounded half (confirmed live, e.g. 2575 -> per-person 1288, not
            # 1287.50) and disagreeing on that account never means an unsupported party.
            total_price = full_price
            if full_price != price * 2:
                notes.append(
                    f"Full price {full_price} disagrees with per-person price x2 "
                    f"({price * 2}); full price is treated as authoritative and the "
                    "per-person figure appears rounded"
                )
        else:
            notes.append(
                f"Party is {adults} adults + {children} children; "
                "total price not derived for an unsupported party"
            )

    country_label = offer.breadcrumbs[0].label if offer.breadcrumbs else None
    country = COUNTRIES.get(country_label) if country_label else None
    destination = " / ".join(b.label for b in offer.breadcrumbs) or None
    airport = AIRPORTS.get(offer.departureAirport)
    board = normalize_board("tui", offer.boardType, offer.boardCode)

    rating = offer.tripAdvisorRating
    if rating is not None and not 1 <= rating <= 5:
        rating = None

    url = urljoin("https://www.tui.pl", offer.offerUrl)
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != "www.tui.pl"
        or not parts.path.startswith("/wypoczynek/")
    ):
        raise ValueError("Unexpected TUI offer URL")

    return Offer(
        provider="tui",
        offer_id=offer.offerCode,
        hotel_name=offer.hotelName,
        country=country,
        destination=destination,
        departure_airport=airport,
        departure_date=departure,
        return_date=return_date,
        number_of_days=days,
        number_of_people=number_of_people,
        price_per_person=price,
        total_price=total_price,
        currency=offer.currency,
        hotel_stars=offer.hotelStandard,
        rating=rating,
        number_of_reviews=offer.tripAdvisorReviewsNo,
        board_type=board,
        url=url,
        found_at=observed_at,
        last_seen=observed_at,
        provider_rating_max=5.0,
        # Never raised to True just because a single-source record is specific:
        # it is not cross-checked against a second document.
        price_is_complete=False,
        variant_verified=False,
        variant_identity=None,
        price_notes="; ".join(notes),
        price_verification_reason=(
            "Single-document search_xhr observation; not independently confirmed "
            "via a detail page or a second source"
        ),
    )


def _search_response_top(body: str) -> dict[str, object]:
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("TUI search/offers response exceeds size limit")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ValueError("Invalid TUI search/offers JSON payload") from exc
    return mapping(data)


def extract_search_response_offers(body: str) -> list[dict[str, object]]:
    """Parse the raw JSON body of `.../search/offers`, passively captured via
    Playwright (never fetched directly by this module), and return its offer
    records. Shape: `{"pagination": {...}, "offers": [...], ...}`.
    """
    top = _search_response_top(body)
    offers = top.get("offers")
    if not isinstance(offers, list):
        raise ValueError("Missing TUI search/offers offers list")
    return [mapping(o) for o in offers]


def extract_search_response_pagination(body: str) -> dict[str, object]:
    """Return the raw `pagination` object from a `search/offers` response.

    Shape: `{"page": 0, "pageSize": 20, "totalResults": ..., "sorting": ...,
    "pagesCount": ...}` -- 0-indexed `page`, `pagesCount` covering the whole
    result set. An absent or malformed `pagination` object returns `{}`;
    callers must treat that as "assume a single page", never guess a count.
    """
    top = _search_response_top(body)
    pagination = top.get("pagination")
    return mapping(pagination) if pagination is not None else {}


def parse_search_response(body: str, observed_at: datetime) -> list[Offer]:
    """Full offline pipeline for the search_xhr route: parse, normalize; skip
    unreadable records. `body` is the response text already captured passively by
    `tui_browser.capture_search_offers`; this function makes no network access."""
    raw_offers = extract_search_response_offers(body)
    result: list[Offer] = []
    for index, raw in enumerate(raw_offers):
        try:
            result.append(normalize_offer(raw, observed_at))
        except (ValidationError, ValueError, KeyError, TypeError) as exc:
            logger.warning("TUI record %s rejected: %s", index, exc)
    if raw_offers and not result:
        raise ValueError("No readable TUI records; possible schema change")
    return result
