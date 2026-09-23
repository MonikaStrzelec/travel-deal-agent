"""Pure parsing of small Rainbow card fragments, without browser or network access."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from ..boards import normalize_board
from ..models import Offer
from .rainbow_config import AIRPORT_LABELS
from .rainbow_errors import RainbowStructureError

# Source labels, not business thresholds. Unknown countries remain unknown.
COUNTRIES = {
    "Turcja": "TR",
    "Grecja": "GR",
    "Tunezja": "TN",
    "Albania": "AL",
    "Egipt": "EG",
    "Hiszpania": "ES",
    "Bułgaria": "BG",
    "Cypr": "CY",
    "Włochy": "IT",
    "Portugalia": "PT",
}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list["Node | str"] = field(default_factory=list)

    def text(self) -> str:
        return " ".join(
            "".join(c.text() if isinstance(c, Node) else c for c in self.children).split()
        )

    def descendants(self) -> list["Node"]:
        return [self] + [n for c in self.children if isinstance(c, Node) for n in c.descendants()]


class CardHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in {"img", "input", "br", "hr", "meta", "link", "source", "wbr", "area"}:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse_price(text: str) -> Decimal:
    normalized = "".join(text.split())
    match = re.fullmatch(r"(\d+(?:[,.]\d{2})?)zł/os\.", normalized)
    if match is None:
        raise RainbowStructureError("Unrecognized Rainbow per-person PLN price")
    amount = Decimal(match[1].replace(",", "."))
    if amount <= 0:
        raise RainbowStructureError("Nonpositive Rainbow price")
    return amount


def parse_card(html: str, observed_at: datetime) -> Offer:
    if len(html) > 250_000:
        raise RainbowStructureError("Rainbow card exceeds diagnostic size limit")
    parser = CardHTML()
    parser.feed(html)
    nodes = parser.root.descendants()
    cards = [n for n in nodes if n.attrs.get("data-test-id", "").startswith("r-bloczek:szukaj:")]
    if len(cards) != 1:
        raise RainbowStructureError("Expected exactly one Rainbow card")
    fields = cards[0].descendants()

    def element(prefix: str, required: bool = False) -> Node | None:
        found = [n for n in fields if n.attrs.get("data-test-id", "").startswith(prefix)]
        if len(found) > 1 or (required and not found):
            raise RainbowStructureError(f"Missing or ambiguous Rainbow field: {prefix}")
        return found[0] if found else None

    def text(name: str, required: bool = False) -> str | None:
        node = element(f"r-typography:szukaj:{name}-", required)
        return node.text() if node else None

    hotel = text("tytul", True)
    if not hotel:
        raise RainbowStructureError("Empty Rainbow hotel name")
    price = parse_price(text("cena-aktualna", True) or "")
    links = [n for n in nodes if n.tag == "a" and any(c is cards[0] for c in n.descendants())]
    if len(links) != 1 or not links[0].attrs.get("href"):
        raise RainbowStructureError("Missing or ambiguous enclosing offer link")
    url = urljoin("https://r.pl", links[0].attrs["href"])
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "r.pl" or parts.path in {"/", "/szukaj"}:
        raise RainbowStructureError("Unexpected Rainbow product URL")
    star_node = element("r-gwiazdki:szukaj:")
    stars = None
    if star_node:
        label = re.fullmatch(
            r"Ocena: ([\d.]+) gwiazd\w* na 5", star_node.attrs.get("aria-label", "")
        )
        value = star_node.attrs.get("data-rating", "")
        if not label or value != label[1] or not re.fullmatch(r"[0-5](?:\.5)?", value):
            raise RainbowStructureError("Inconsistent Rainbow stars")
        stars = float(value)
        if stars > 5:
            raise RainbowStructureError("Rainbow stars outside scale")
    rating_node = element("r-typography:szukaj:ocena-")
    rating, reviews = None, None
    if rating_node:
        match = re.fullmatch(r"(\d+(?:\.\d+)?)/6(?:\s*\((\d+) opini\w*\))?", rating_node.text())
        if not match or not 0 <= float(match[1]) <= 6:
            raise RainbowStructureError("Unrecognized Rainbow rating / review count")
        rating = float(match[1])
        reviews = int(match[2]) if match[2] is not None else None
        aria = rating_node.attrs.get("aria-label")
        if aria:
            labeled = re.fullmatch(
                r"Ocena produktu: (\d+(?:\.\d+)?) na 6(?:, (\d+) opini\w*)?", aria
            )
            if not labeled or (
                float(labeled[1]),
                int(labeled[2]) if labeled[2] is not None else None,
            ) != (rating, reviews):
                raise RainbowStructureError("Conflicting Rainbow rating label")
    date_text = text("termin-wyjazdu")
    departure, days = None, None
    if date_text:
        match = re.fullmatch(r"(\d{2}\.\d{2}\.\d{4}) \((\d+) dni / (\d+) nocleg\w*\)", date_text)
        if not match:
            raise RainbowStructureError("Unrecognized Rainbow date/duration")
        try:
            departure = datetime.strptime(match[1], "%d.%m.%Y").date()
        except ValueError as exc:
            raise RainbowStructureError("Invalid Rainbow departure date") from exc
        days = int(match[2])
        if days < 1 or not 0 <= int(match[3]) <= days:
            raise RainbowStructureError("Invalid Rainbow days/nights")
    location = text("lokalizacja")
    country_label = (location or "").partition("•")[2].strip().partition(":")[0].strip()
    country = COUNTRIES.get(country_label)
    airport_text, board_text = text("przystanek"), text("wyzywienie")
    airport = {label: code for code, label in AIRPORT_LABELS.items()}.get(airport_text or "")
    board = normalize_board("rainbow", board_text)
    # (+N) summaries do not identify the airport/board attached to the displayed price.
    evidence = {
        "url": url,
        "hotel": hotel,
        "location": location,
        "date": date_text,
        "airport": airport_text,
        "board": board_text,
        "people": 2,
        "currency": "PLN",
    }
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    return Offer(
        provider="rainbow",
        offer_id="listing-" + digest,
        hotel_name=hotel,
        country=country,
        destination=location,
        departure_airport=airport,
        departure_date=departure,
        return_date=None,
        number_of_days=days,
        number_of_people=2,
        price_per_person=price,
        total_price=price * 2,
        currency="PLN",
        hotel_stars=stars,
        rating=rating,
        number_of_reviews=reviews,
        board_type=board,
        url=url,
        found_at=observed_at,
        last_seen=observed_at,
        provider_rating_max=6,
        price_is_complete=False,
        variant_verified=False,
        variant_identity=None,
        price_notes="Listing estimate only; total = displayed per-person price × 2. "
        f"Airport: {airport_text!r}; board: {board_text!r}. "
        "Product URL and summary do not establish a booking variant.",
        price_verification_reason="Unverified listing price and aggregate variant",
    )
