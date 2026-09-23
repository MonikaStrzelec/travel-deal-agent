"""Offline TUI parsing for two confirmed sources; normalization is shared.

Two sources are confirmed offline, sharing the same offer field names but not the
same duration semantics (see `Source` and `normalize_offer`):

- `category_ssr`: `/wypoczynek/<country>/oferty-last-minute`, a server-rendered
  (Next.js) page whose `<script id="__NEXT_DATA__">` text is base64-encoded JSON
  (unlike ITAKA's plain-JSON `__NEXT_DATA__`); `initialOffersData` holds one batch
  with real offers embedded directly in the HTML. Parsed via `parse_listing`.
- `search_xhr`: the body of `.../api/services/tui-search/api/search/offers`,
  observed by passively listening to responses after opening a filtered
  `build_search_path` URL in a real browser (`tui_browser.capture_search_offers`);
  this module never calls that endpoint directly. This is the **production** source:
  the equivalent filtered results page
  (`/wypoczynek/wyniki-wyszukiwania-samolot?q=...`) was confirmed, in one prior
  robots-respecting GET, to server-render only a loading skeleton with an empty
  `initialOffersData` -- no offers are ever obtained from that route via plain HTTP.
  Parsed via `parse_search_response`.

Where offers ARE present on either source, each already represents one specific
bookable configuration, not a summary of several options: reconnaissance found 20
offers with 20 distinct `hotelCode`/`offerCode` values on one category_ssr page, and
13 distinct values on one search_xhr response, no nested alternative airport/board
arrays, and no aggregate/ambiguous card.

`offerCode` is a long, structured, human-decodable string (airports, dates, hotel and
room codes); it is used directly as `Offer.offer_id`. Its stability across repricing or
repeated scans has not been independently verified (only observed once per source).
"""

import base64
import binascii
import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin, urlsplit

from pydantic import Field, ValidationError

from ..boards import normalize_board
from ..models import Offer
from .itaka_data import Boundary, mapping

logger = logging.getLogger(__name__)

MAX_HTML_BYTES = 4_000_000

# Confirmed via reconnaissance of the airport dropdown's `availableAirports` data.
# Warszawa-Modlin was listed but disabled on the source site; kept for completeness.
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
# Source labels, not business thresholds. "Turcja", "Bułgaria" and "Malta" were
# directly observed on real cards (2026-09-22 reconnaissance); the rest are
# standard Polish country names (same convention already used for Rainbow) kept
# for practical coverage. Unknown labels remain unknown.
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

# Confirmed offline from two sources sharing the same field names:
# - "category_ssr": the SSR `__NEXT_DATA__` embedded in `/wypoczynek/<country>/
#   oferty-last-minute`; `hotelStandard` observed as a plain int.
# - "search_xhr": the body of `.../api/services/tui-search/api/search/offers`,
#   passively captured via Playwright after opening a `build_search_path` URL;
#   `hotelStandard` observed as a float, including a genuine half-star value (3.5).
# The two sources also disagree on how `duration` relates to the departure/return
# dates (see `normalize_offer`); nothing here assumes one rule fits both.
Source = Literal["category_ssr", "search_xhr"]


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


class NextDataScript(HTMLParser):
    """Collect only the __NEXT_DATA__ script's text; never execute page scripts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inside = False
        self.parts: list[str] = []
        self.scripts = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.inside = True
            self.scripts += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.inside = False

    def handle_data(self, data: str) -> None:
        if self.inside:
            self.parts.append(data)


def decode_next_data(html: str) -> dict[str, object]:
    """Locate the single __NEXT_DATA__ script and return its decoded JSON object."""
    if len(html) > MAX_HTML_BYTES:
        raise ValueError("TUI listing HTML exceeds size limit")
    parser = NextDataScript()
    parser.feed(html)
    if parser.scripts != 1:
        raise ValueError("Missing or ambiguous TUI __NEXT_DATA__ script; possible access challenge")
    raw_text = "".join(parser.parts).strip()
    if not raw_text:
        raise ValueError("Empty TUI __NEXT_DATA__ payload")
    try:
        decoded = base64.b64decode(raw_text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid TUI __NEXT_DATA__ base64 payload") from exc
    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Invalid TUI __NEXT_DATA__ text encoding") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError("Invalid TUI __NEXT_DATA__ JSON payload") from exc
    return mapping(data)


def extract_offers(next_data: dict[str, object]) -> list[dict[str, object]]:
    """Return the raw offer records from pageProps.initialOffersData[*].offers.

    Two routes are confirmed offline (both share the same pageProps schema):

    - `/wypoczynek/<country>/oferty-last-minute`: `initialOffersData` is a list with
      exactly one batch `{"pagination": {...}, "offers": [...]}`.
    - `/wypoczynek/wyniki-wyszukiwania-samolot?q=...`: one live, robots-respecting GET
      (with our production filters applied) returned `initialOffersData: []` -- zero
      batches -- alongside `initialOffersKey`/`initialFiltersData`/
      `initialGlobalStateReduxSlice` all `None`, and a server-rendered loading
      skeleton (`results-container--loading`, `offer-tile-skeleton` markup, no
      "brak wynikow" or similar empty-state text). This is strong, direct evidence
      that this route does not server-render offers at all for a query built from
      our filters: real results there are fetched client-side (from the
      robots-disallowed `/api/services/tui-search/api/search/offers`), not
      embedded in this HTML. Confirmed only as *empty*; a populated shape for this
      route has not been observed and is not assumed here.

    An empty `initialOffersData` list is therefore treated as zero offers, not an
    error: nothing is silently dropped (there is nothing to normalize), and the
    provider completes its cycle normally rather than crashing. More than one batch
    remains rejected outright -- picking one would be a guess, never made here.
    """
    props = mapping(next_data.get("props"))
    page_props = mapping(props.get("pageProps"))
    batches = page_props.get("initialOffersData")
    if not isinstance(batches, list):
        raise ValueError("Missing TUI initialOffersData")
    if len(batches) > 1:
        raise ValueError("Ambiguous TUI initialOffersData: more than one batch")
    if not batches:
        return []
    offers = mapping(batches[0]).get("offers")
    if not isinstance(offers, list):
        raise ValueError("Missing TUI offers list")
    return [mapping(o) for o in offers]


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


def normalize_offer(raw: dict[str, object], observed_at: datetime, *, source: Source) -> Offer:
    """Normalize one already-specific TUI variant; never split or combine options.

    `source` selects the one confirmed date/duration relationship for that source;
    see the `Source` constant above. Never averaged or guessed across sources.
    """
    offer = TuiRawOffer.model_validate(raw)
    if offer.soldOut:
        raise ValueError("TUI offer is marked sold out")
    departure = _date(offer.departureDate, "departure date")
    return_date = _date(offer.returnDate, "return date")
    if return_date <= departure:
        raise ValueError("TUI return date does not follow departure date")
    days = (return_date - departure).days
    if source == "category_ssr" and days != offer.duration + 1:
        raise ValueError(
            "TUI nights disagree with departure/return dates "
            "(category_ssr: expected days == duration + 1)"
        )
    elif source == "search_xhr" and days != offer.duration:
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
        # neither source is independently cross-checked against a second document.
        price_is_complete=False,
        variant_verified=False,
        variant_identity=None,
        price_notes="; ".join(notes),
        price_verification_reason=(
            f"Single-document {source} observation; not independently confirmed "
            "via a detail page or a second source"
        ),
    )


def _search_response_top(body: str) -> dict[str, object]:
    if len(body) > MAX_HTML_BYTES:
        raise ValueError("TUI search/offers response exceeds size limit")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ValueError("Invalid TUI search/offers JSON payload") from exc
    return mapping(data)


def extract_search_response_offers(body: str) -> list[dict[str, object]]:
    """Parse the raw JSON body of `.../search/offers`, passively captured via
    Playwright (never fetched directly by this module), and return its offer
    records. Shape: `{"pagination": {...}, "offers": [...], ...}` -- the response
    IS the batch, unlike `initialOffersData`'s list-of-batches on the SSR route.
    """
    top = _search_response_top(body)
    offers = top.get("offers")
    if not isinstance(offers, list):
        raise ValueError("Missing TUI search/offers offers list")
    return [mapping(o) for o in offers]


def extract_search_response_pagination(body: str) -> dict[str, object]:
    """Return the raw `pagination` object from a `search/offers` response.

    Confirmed live (2026-09-22 reconnaissance, broad query): `{"page": 0,
    "pageSize": 20, "totalResults": 3469, "sorting": "price", "pagesCount":
    174}` -- 0-indexed `page`, `pagesCount` covering the whole result set. An
    absent or malformed `pagination` object returns `{}`; callers must treat
    that as "assume a single page", never guess a count.
    """
    top = _search_response_top(body)
    pagination = top.get("pagination")
    return mapping(pagination) if pagination is not None else {}


def parse_listing(html: str, observed_at: datetime) -> list[Offer]:
    """Full offline pipeline for the category_ssr route: decode, extract, normalize;
    skip unreadable records."""
    next_data = decode_next_data(html)
    raw_offers = extract_offers(next_data)
    result: list[Offer] = []
    for index, raw in enumerate(raw_offers):
        try:
            result.append(normalize_offer(raw, observed_at, source="category_ssr"))
        except (ValidationError, ValueError, KeyError, TypeError) as exc:
            logger.warning("TUI record %s rejected: %s", index, exc)
    if raw_offers and not result:
        raise ValueError("No readable TUI records; possible schema change")
    return result


def parse_search_response(body: str, observed_at: datetime) -> list[Offer]:
    """Full offline pipeline for the search_xhr route: parse, normalize; skip
    unreadable records. `body` is the response text already captured passively by
    `tui_browser.capture_search_offers`; this function makes no network access."""
    raw_offers = extract_search_response_offers(body)
    result: list[Offer] = []
    for index, raw in enumerate(raw_offers):
        try:
            result.append(normalize_offer(raw, observed_at, source="search_xhr"))
        except (ValidationError, ValueError, KeyError, TypeError) as exc:
            logger.warning("TUI record %s rejected: %s", index, exc)
    if raw_offers and not result:
        raise ValueError("No readable TUI records; possible schema change")
    return result
