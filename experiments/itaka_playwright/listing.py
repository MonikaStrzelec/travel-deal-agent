"""Bounded listing shortlist; no detail requests or booking-price claims."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Protocol
from urllib.parse import parse_qs, urljoin, urlsplit

from playwright.sync_api import Locator, Page

from experiments.itaka_playwright.settings import PocLimits
from travel_deal_agent.boards import normalize_board
from travel_deal_agent.config_types import FilterConfig
from travel_deal_agent.filtering import matches_criteria
from travel_deal_agent.models import Offer
from travel_deal_agent.providers.itaka_data import AIRPORTS, COUNTRIES

Card = dict[str, str | None]


def optional_text(locator: Locator) -> str | None:
    return locator.inner_text() if locator.count() == 1 else None


def read_card(card: Locator) -> Card:
    """Read evidence once; absent fields are not guessed."""
    stars = card.get_by_test_id("rating-stars")
    return {
        "text": card.inner_text(),
        "url": card.get_by_test_id("offer-list-item-button").get_attribute("href"),
        "hotel_name": optional_text(card.get_by_role("heading", level=3)),
        "destination": optional_text(card.get_by_test_id("offer-list-item-destination")),
        "price_text": optional_text(card.get_by_test_id("current-price")),
        "rating_text": optional_text(card.get_by_test_id("reviews-rating")),
        "star_icons": str(stars.get_by_role("listitem").count()) if stars.count() == 1 else None,
    }


def listing_offer(card: Card) -> Offer:
    """Normalize observed card wording conservatively; keep the price unverified."""
    url = urljoin("https://www.itaka.pl", card.get("url") or "")
    parts = urlsplit(url)
    path = parts.path.strip("/").split("/")
    text = card.get("text") or ""
    lines = [line.strip() for line in text.splitlines()]
    price_match = re.fullmatch(
        r"\s*([\d\s]+(?:[,.]\d{2})?)\s*zł\s*/os\.\s*", card.get("price_text") or ""
    )
    price = Decimal(re.sub(r"\s", "", price_match[1]).replace(",", ".")) if price_match else None
    # Include the explicitly displayed per-person supplement for preliminary thresholds.
    supplements = re.findall(r"\+(\d+) zł \(TFG i TFP\)", text)
    if price is not None:
        price += sum((Decimal(value) for value in supplements), Decimal(0))
    dates = re.search(
        r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s*-\s*"
        r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s*\((\d+) dni\)",
        text,
    )
    departure = returning = None
    if dates:
        end_year = int(dates[6])
        start_year = int(dates[3]) if dates[3] else end_year - (int(dates[2]) > int(dates[5]))
        departure = date(start_year, int(dates[2]), int(dates[1]))
        returning = date(end_year, int(dates[5]), int(dates[4]))
    airports = {
        AIRPORTS[city]
        for city in AIRPORTS
        for line in lines
        if re.fullmatch(re.escape(city) + r"\s+\d{1,2}:\d{2}", line)
    }
    boards = {board for line in lines if (board := normalize_board("itaka", line)) is not None}
    rating = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*/6\s*", card.get("rating_text") or "")
    reviews = re.search(r"(?m)^(\d+) opinii\s*$", text)
    party = parse_qs(parts.query).get("adults[0]")
    return Offer(
        provider="itaka",
        offer_id=url,
        url=url,
        hotel_name=card.get("hotel_name"),
        country=COUNTRIES.get(path[1]) if len(path) > 2 and path[0] == "wczasy" else None,
        destination=card.get("destination"),
        departure_date=departure,
        return_date=returning,
        number_of_days=int(dates[7]) if dates else None,
        departure_airport=next(iter(airports)) if len(airports) == 1 else None,
        number_of_people=2 if party == ["2"] else None,
        price_per_person=price,
        currency="PLN" if price_match else None,
        hotel_stars=float(card.get("star_icons") or "0") if card.get("star_icons") else None,
        board_type=next(iter(boards)) if len(boards) == 1 else None,
        rating=float(rating[1].replace(",", ".")) if rating else None,
        number_of_reviews=int(reviews[1]) if reviews else None,
        price_is_complete=False,
        variant_verified=False,
        price_notes="Listing estimate including displayed TFG/TFP; booking total unverified",
    )


def qualifies(card: Card, filters: FilterConfig, today: date) -> bool:
    try:
        return matches_criteria(listing_offer(card), filters, today)
    except (ValueError, InvalidOperation):
        return False


class CardSource(Protocol):
    def keys(self) -> list[str]: ...
    def read(self, key: str) -> Card: ...
    def advance(self) -> None: ...


@dataclass
class Collection:
    candidates: list[Card] = field(default_factory=list)
    analyzed: int = 0
    scrolls: int = 0
    stop_reason: str = ""


def collect_candidates(
    source: CardSource, limits: PocLimits, eligible: Callable[[Card], bool]
) -> Collection:
    """Evaluate each variant once; stop before further reading/scrolling at either cap."""
    result = Collection()
    seen: set[str] = set()
    idle = 0
    while True:
        new_keys = [key for key in dict.fromkeys(source.keys()) if key not in seen]
        idle = 0 if new_keys else idle + 1
        for key in new_keys:
            seen.add(key)
            card = source.read(key)
            result.analyzed += 1
            if eligible(card):
                result.candidates.append(card)
            if len(result.candidates) >= limits.max_offers:
                result.stop_reason = "candidate_limit"
                return result
            if result.analyzed >= limits.max_analyzed_cards:
                result.stop_reason = "analysis_limit"
                return result
        if idle >= limits.max_idle_scrolls:
            result.stop_reason = "no_new_cards"
            return result
        if result.scrolls >= limits.max_scrolls:
            result.stop_reason = "scroll_limit"
            return result
        source.advance()
        result.scrolls += 1


class BrowserCards:
    def __init__(self, page: Page, limits: PocLimits) -> None:
        self.page = page
        self.limits = limits
        self.locators: dict[str, Locator] = {}

    def keys(self) -> list[str]:
        self.locators = {}
        cards = self.page.get_by_test_id("offer-list-item")
        for index in range(cards.count()):
            card = cards.nth(index)
            href = card.get_by_test_id("offer-list-item-button").get_attribute("href")
            # Preserve the complete variant query; never deduplicate by hotel alone.
            key = urljoin(self.page.url, href) if href else "missing-url:" + str(index)
            self.locators.setdefault(key, card)
        return list(self.locators)

    def read(self, key: str) -> Card:
        return read_card(self.locators[key])

    def advance(self) -> None:
        # One small viewport movement; no pagination, details or per-card requests.
        self.page.mouse.wheel(0, 600)
        self.page.wait_for_timeout(self.limits.scroll_wait_ms)
