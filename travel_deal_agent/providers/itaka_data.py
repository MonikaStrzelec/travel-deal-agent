"""Validate the observed ITAKA SSR boundary and normalize diagnostic offers."""

import hashlib
import json
import logging
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..boards import normalize_board
from ..models import Offer

logger = logging.getLogger(__name__)


class Boundary(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")


class Named(Boundary):
    title: str
    id: str | None = None


class Payment(Boundary):
    type: str
    amount: int = Field(ge=0)


class Participant(Boundary):
    price: int = Field(ge=0)
    type: str
    additionalPayments: list[Payment] | None = None


class Group(Boundary):
    price: int = Field(ge=0)
    rateId: str = Field(min_length=1)
    participants: list[Participant]


class Room(Boundary):
    baseRoomCode: str | None = None
    meal: Named
    room: Named


class Reviews(Boundary):
    customersRating: float | None = None
    reviewsNumber: int | None = Field(default=None, ge=0)


class Location(Named):
    type: str


class Content(Boundary):
    title: str
    hotelRating: int | None = None
    reviews: Reviews = Field(default_factory=Reviews)
    geographicalIdentifiers: list[Location] = Field(default_factory=list)


class Segment(Boundary):
    type: str
    beginDate: str
    endDate: str
    beginTime: str | None = None
    endTime: str | None = None
    departure: Named | None = None
    destination: Named | None = None
    participantGroups: list[Room] = Field(default_factory=list)
    content: Content | None = None


class Duration(Boundary):
    days: int = Field(ge=1)


class Rate(Boundary):
    supplier: str
    supplierObjectId: str
    rateType: str
    currency: str
    participantGroups: list[Group]
    duration: Duration
    segments: list[Segment]


class NextData(HTMLParser):
    """Collect data scripts and actual detail links; never execute page scripts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inside = False
        self.parts: list[str] = []
        self.links: list[str] = []
        self.scripts = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("id") == "__NEXT_DATA__":
            self.inside = True
            self.scripts += 1
        if tag == "a" and values.get("href"):
            self.links.append(values["href"] or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.inside = False

    def handle_data(self, data: str) -> None:
        if self.inside:
            self.parts.append(data)


class MultiRates(Boundary):
    list: list[dict[str, object]]
    ratesCount: int = Field(ge=0)


class MainData(Boundary):
    multiRoomRates: MultiRates


class Data(Boundary):
    main: MainData


class State(Boundary):
    data: Data


class Query(Boundary):
    state: State
    queryKey: list[object]


class ParsedPage(Boundary):
    rates: list[dict[str, object]]
    count: int
    skip: int
    take: int
    links: list[str]


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError("Expected a JSON object")
    return {str(k): v for k, v in value.items()}


def parse_page(html: str) -> ParsedPage:
    parser = NextData()
    parser.feed(html)
    if parser.scripts != 1:
        raise ValueError("Missing or ambiguous ITAKA data; possible access challenge")
    raw: object = json.loads("".join(parser.parts))
    page = mapping(mapping(mapping(raw)["props"])["pageProps"])
    queries = mapping(page["initialQueryState"])["queries"]
    if not isinstance(queries, list):
        raise ValueError("Missing query list")
    candidates = [
        mapping(q)
        for q in queries
        if isinstance(q, dict)
        and isinstance(q.get("queryKey"), list)
        and q["queryKey"][:1] == ["rates"]
    ]
    if len(candidates) != 1:
        raise ValueError("Missing or ambiguous rates query")
    query = Query.model_validate(candidates[0])
    if len(query.queryKey) < 2:
        raise ValueError("Missing pagination options")
    options = mapping(query.queryKey[1])
    skip, take = options.get("skip"), options.get("take")
    if type(skip) is not int or type(take) is not int or skip < 0 or take < 1:
        raise ValueError("Invalid pagination metadata")
    data = query.state.data.main.multiRoomRates
    if len(data.list) > take or (not data.list and skip < data.ratesCount):
        raise ValueError("Inconsistent ITAKA page")
    return ParsedPage(
        rates=data.list, count=data.ratesCount, skip=skip, take=take, links=parser.links
    )


# Normalization dictionaries do not restrict requested destinations. Unknown locations
# remain diagnostic until their source identifier can be mapped confidently.
COUNTRIES = dict(
    pair.split(":")
    for pair in [
        "albania:AL",
        "bulgaria:BG",
        "chorwacja:HR",
        "turcja:TR",
        "bialorus:BY",
        "czarnogora:ME",
        "litwa:LT",
        "lotwa:LV",
        "moldawia:MD",
        "serbia:RS",
        "kosowo:XK",
        "grecja:GR",
        "hiszpania:ES",
        "wyspy-kanaryjskie:ES",
        "wlochy:IT",
        "portugalia:PT",
        "cypr:CY",
        "malta:MT",
        "francja:FR",
        "polska:PL",
        "egipt:EG",
        "tunezja:TN",
        "maroko:MA",
        "kenia:KE",
        "tanzania:TZ",
        "zanzibar:TZ",
        "mauritius:MU",
        "seszele:SC",
        "madagaskar:MG",
        "wyspy-zielonego-przyladka:CV",
        "senegal:SN",
        "gambia:GM",
        "rpa:ZA",
        "algieria:DZ",
        "namibia:NA",
        "tajlandia:TH",
        "malediwy:MV",
        "sri-lanka:LK",
        "oman:OM",
        "zjednoczone-emiraty-arabskie:AE",
        "dominikana:DO",
        "meksyk:MX",
        "kuba:CU",
        "indonezja:ID",
        "wietnam:VN",
        "japonia:JP",
        "chiny:CN",
        "gruzja:GE",
        "armenia:AM",
        "jordania:JO",
        "izrael:IL",
        "austria:AT",
        "czechy:CZ",
        "niemcy:DE",
        "slowacja:SK",
        "slowenia:SI",
        "rumunia:RO",
        "wegry:HU",
        "estonia:EE",
        "islandia:IS",
        "norwegia:NO",
        "szwecja:SE",
        "finlandia:FI",
        "dania:DK",
        "irlandia:IE",
        "wielka-brytania:GB",
        "szwajcaria:CH",
        "usa:US",
        "kanada:CA",
        "brazylia:BR",
        "argentyna:AR",
        "peru:PE",
        "australia:AU",
        "nowa-zelandia:NZ",
    ]
)
AIRPORTS = {
    "Łódź": "LCJ",
    "Warszawa-Okęcie": "WAW",
    "Warszawa Chopin": "WAW",
    "Warszawa-Modlin": "WMI",
    "Katowice": "KTW",
    "Wrocław": "WRO",
    # tests/fixtures/itaka/FUERIOC_mapping.json (real detail reconnaissance data):
    # a Journey object `{"id": "POZ", "title": "Poznań", "beginDateTime": ...}`
    # directly ties this title to this IATA code (itaka_details.py cross-checks
    # `journey.id` against the departure FlightPoint's own `place.code`).
    "Poznań": "POZ",
}


def known_price(group: Group) -> tuple[Decimal | None, Decimal | None, str]:
    """Return a diagnostic lower bound, never a claim of complete holiday cost."""
    if len(group.participants) != 2 or any(p.type != "adult" for p in group.participants):
        raise ValueError("Only two adults are supported")
    if sum(p.price for p in group.participants) != group.price:
        return None, None, "Group and participant prices disagree"
    if any(p.additionalPayments is None for p in group.participants):
        return None, None, "Additional payment data missing"
    totals = []
    for person in group.participants:
        payments = person.additionalPayments or []
        types = [p.type for p in payments]
        if len(types) != len(set(types)) or any(t not in {"TFG", "TFP"} for t in types):
            return None, None, "Unknown or duplicate payment type"
        totals.append(person.price + sum(p.amount for p in payments))
    total = Decimal(sum(totals)) / 100
    return (
        total / 2,
        total,
        "Listing price including listed TFG/TFP; booking variant and price not verified",
    )


def normalize_rate(raw: dict[str, object], links: list[str]) -> Offer:
    rate = Rate.model_validate(raw)
    if (
        rate.supplier != "itaka"
        or rate.rateType != "holidays"
        or len(rate.participantGroups) != 1
        or [s.type for s in rate.segments] != ["flight", "hotel", "flight"]
    ):
        raise ValueError("Unsupported package structure")
    outbound, hotel, inbound = rate.segments
    if hotel.content is None or len(hotel.participantGroups) != 1:
        raise ValueError("Missing or ambiguous hotel")
    content = hotel.content
    room = hotel.participantGroups[0]
    group = rate.participantGroups[0]
    # Include the opaque source id AND semantic variant fields. Price is excluded.
    # A changed source id splits history; no speculative cross-id merging occurs.
    raw_segments = raw["segments"]
    if not isinstance(raw_segments, list):
        raise ValueError("Expected segment list")
    # Preserve unmodeled flight/room fields too; descriptive hotel content is volatile.
    segments = [{k: v for k, v in mapping(s).items() if k != "content"} for s in raw_segments]
    identity = {
        "rate": group.rateId,
        "hotel": rate.supplierObjectId,
        "currency": rate.currency,
        "segments": segments,
        "brand": raw.get("brand"),
        "people": [p.type for p in group.participants],
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    country_items = [v for v in content.geographicalIdentifiers if v.type == "country"]
    country_id = country_items[0].id if len(country_items) == 1 else None
    country = COUNTRIES.get(country_id or "")
    regions = [v.title for v in content.geographicalIdentifiers if v.type == "province"]
    price, total, notes = known_price(group)
    url = next(
        (
            urljoin("https://www.itaka.pl", link)
            for link in links
            if urlsplit(urljoin("https://www.itaka.pl", link)).netloc == "www.itaka.pl"
            and urlsplit(link).path.startswith("/wczasy/")
            and parse_qs(urlsplit(link).query).get("id[0]") == [group.rateId]
        ),
        None,
    )
    rating = content.reviews.customersRating
    if rating is not None and not 1 <= rating <= 6:
        rating = None
    board = normalize_board("itaka", room.meal.title, room.meal.id)
    return Offer(
        provider="itaka",
        offer_id=digest,
        variant_identity=digest,
        hotel_name=content.title,
        country=country,
        destination=" / ".join(regions) or None,
        departure_airport=AIRPORTS.get(outbound.departure.title if outbound.departure else ""),
        departure_date=date.fromisoformat(outbound.beginDate),
        return_date=date.fromisoformat(inbound.endDate),
        number_of_days=rate.duration.days,
        number_of_people=2,
        price_per_person=price,
        total_price=total,
        currency=rate.currency,
        hotel_stars={30: 3.0, 40: 4.0, 50: 5.0}.get(content.hotelRating or 0),
        rating=rating,
        number_of_reviews=content.reviews.reviewsNumber,
        board_type=board,
        url=url,
        price_is_complete=False,
        price_notes=notes,
    )


def normalize_page(page: ParsedPage) -> list[Offer]:
    result = []
    for index, raw in enumerate(page.rates):
        try:
            result.append(normalize_rate(raw, page.links))
        except (ValidationError, ValueError, KeyError, TypeError):
            logger.warning("ITAKA record %s rejected: unsupported or malformed data", index)
    if page.rates and not result:
        raise ValueError("No readable ITAKA records; possible schema change")
    return result
