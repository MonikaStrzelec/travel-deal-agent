"""Offline Wakacje.pl parsing for the listing-only source (no detail confirmation).

Confirmed offline via `experiments/wakacje_pl/RECONNAISSANCE.md` (sections referenced
below); nothing here performs or assumes a live request.

The listing page (`/wczasy/` -- the site's own general search, not the narrower
`/lastminute/` category -- optionally with one robots-legal single-flag query such as
`?z-wroclawia`, or that same flag combined with the site's own confirmed pagination
shape, `?str-<n>,z-wroclawia`) embeds a standard Next.js `<script id="__NEXT_DATA__">`
whose text is plain JSON (unlike TUI's base64-wrapped payload). The offer records
live at `props.dehydratedState.queries[?].state.data.offers.data`, in the one query
whose `queryKey[0] == "listingOffers"` (sec 3, 11.3). This shape, and this module's
parsing of it, is identical regardless of which page of which confirmed airport it
came from -- pagination is an orchestration concern of `wakacje.py`'s `fetch()`, not
of this module.

Two facts drive this module's shape:

- The bare, robots-legal detail page (`/oferty/.../slug-id.html`, no query string)
  carries NO offer-specific data at all -- no price, dates, board, airport or rating
  count (sec 12.2). There is therefore no detail-confirmation stage here, unlike
  ITAKA's `itaka_details.py`; `price_is_complete` stays `False` unconditionally.
- A single-flag departure-airport filter (`?z-<city>`) was confirmed to genuinely
  change server-rendered results, and every offer it returns is unambiguously priced
  for that one airport (`departurePlaces` collapses to a single entry) -- but the
  SAME numeric `id`, fetched under a different airport filter, was observed with a
  DIFFERENT departure date and price (sec 12.1). The raw numeric `id` is therefore
  never used alone as `Offer.offer_id`; see `variant_identity`.
- The provider (`wakacje.py`) now always fetches its one confirmed combined search
  query, which includes the site's own `za-osobe` ("average per person") price-view
  toggle -- the reverse of the query this module's price handling originally
  assumed (see the price section below, sec 24-25).

`departurePlaces` (the list of *other* cities a card could also depart from) is read
only to validate the source shape -- it is never iterated to fabricate additional
offers (sec 6, 8a.5, 12.1: it carries no per-city price or code, and its own contents
are contextual to the specific fetch, not a fixed hotel property).

`ratingReservationCount`'s exact semantics (booking count vs. review count) are not
literally confirmed (sec 8b.1); it is parsed for shape validation only and never
copied into `Offer.number_of_reviews`.
"""

import hashlib
import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from html.parser import HTMLParser

from pydantic import Field, ValidationError

from ..models import Offer
from .itaka_data import Boundary, mapping

logger = logging.getLogger(__name__)

MAX_HTML_BYTES = 4_000_000
BASE = "https://www.wakacje.pl"

# Every fetch this provider makes uses Wakacje.pl's own unmodified default room
# composition (rooms=[{"adult": 2, "kid": 0}]) -- confirmed identical across every
# listing fetch so far (RECONNAISSANCE.md sec 3, 5, 12.1, 25). No per-offer
# party-size field exists on the source record itself, so this is a property of
# how this provider queries, not something read from each offer.
ASSUMED_PARTY_SIZE = 2

# Only country slugs directly observed as a real Wakacje.pl URL path segment.
# The source carries no ISO code (only its own slug, Polish display name and an
# internal numeric id), so an unknown slug is never guessed into a code -- same
# convention as `itaka_data.COUNTRIES`. An unmapped country is NOT a reason to
# reject an offer: business eligibility no longer depends on the country, so such
# an offer keeps `country=None` and its source display name is kept in
# `destination` instead. The slug is the site's own `place.country.slug`, which
# `normalize_offer`'s own `url` field construction confirms is identical to the
# first path segment of a real offer detail URL (`/oferty/<country-slug>/...`).
#
# turcja/egipt/tunezja/grecja -- RECONNAISSANCE.md sec 11.4, 12.1 (offline).
# albania -- live-verified: two real, fetched offer URLs in a later session both
#   began `/oferty/albania/...` (hotel "Alion", two separate date/price variants).
# bulgaria -- RECONNAISSANCE.md sec 11.4: the real, on-page category link
#   `/lastminute/bulgaria/` (offline, not guessed).
# hiszpania -- RECONNAISSANCE.md sec 11.4 (`/lastminute/hiszpania/`) *and*
#   live-verified separately via a real fetched offer URL
#   (`/oferty/hiszpania/...`, hotel "HTop Olympic").
# cypr -- live-verified: the real `place.country.slug` on 2 records from the
#   final full-provider live scan (RECONNAISSANCE.md sec 23), previously
#   unmapped and normalized as country=None.
# malta -- live-verified: the real `place.country.slug` ("malta") on a real
#   offer record (hotel "Luna Holiday Complex") from the confirmed combined
#   search query's live confirmation (sec 25). Previously Malta was seen only
#   as a display name on listing cards, never as a slug -- this is the first
#   time the actual slug was observed, so it is now added, same evidence
#   standard as "cypr" above.
COUNTRIES = {
    "turcja": "TR",
    "egipt": "EG",
    "tunezja": "TN",
    "grecja": "GR",
    "albania": "AL",
    "bulgaria": "BG",
    "hiszpania": "ES",
    "cypr": "CY",
    "malta": "MT",
}

# Confirmed via the site's own `cateringList` filter definition (RECONNAISSANCE.md
# sec 8a.2, 11.3): the numeric `service` code is the reliable board bucket;
# `serviceDesc` free text is only a finer label WITHIN that bucket (e.g. "Ultra All
# Inclusive" vs. "All Inclusive", both code 1) -- never a sign the code itself is
# ambiguous. Code 4 (wlasne/self-catering) maps to RO, matching
# `boards.CANONICAL_BOARDS`. Code 5 ("Według programu" / itinerary-based board) is
# a deliberate business decision to accept ZO as a normal, canonical board
# (boards.CANONICAL_BOARDS, boards.BOARD_ORDER) -- ranked below HB/FB/AI (never
# treated as better), still eligible for filtering.matches() and ranking like any
# other board once "ZO" is present in filters["allowed_boards"].
SERVICE_BOARDS: dict[int, str | None] = {
    1: "AI",
    2: "HB",
    3: "BB",
    4: "RO",
    5: "ZO",
    6: "FB",
}

_AIRPORT_CODE = re.compile(r"[A-Z]{3,4}")
_SLUG = re.compile(r"[a-z0-9-]+")


class GeoItem(Boundary):
    name: str = Field(min_length=1)
    slug: str = Field(min_length=1)


class Place(Boundary):
    country: GeoItem
    region: GeoItem
    city: GeoItem


class RawOffer(Boundary):
    """Only the fields this parser uses; everything else is ignored, not dropped."""

    id: int = Field(gt=0)
    name: str = Field(min_length=1)
    urlName: str = Field(min_length=1)
    place: Place
    price: int = Field(ge=0)
    originalCurrency: str = Field(min_length=1)
    category: float = Field(ge=0, le=5)
    ratingValue: float | None = None
    ratingReservationCount: int | None = Field(default=None, ge=0)
    service: int
    serviceDesc: str = Field(min_length=1)
    departureDate: str
    returnDate: str
    duration: int = Field(ge=1)
    departurePlace: str = Field(min_length=1)
    departurePlaceCode: str = Field(min_length=1)
    # Read for shape validation only -- never iterated to create additional offers.
    departurePlaces: list[str] = Field(default_factory=list)


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


class OfferLinkParser(HTMLParser):
    """Collect each offer card's own real detail-page href, keyed by its id.

    Confirmed offline (`data/wakacje-recon/wczasy-combo-recon.html`, a real fetch
    of this provider's own confirmed search query): each offer card's anchor
    carries `data-test-offer-id="<id>"` and an `href` that keeps the exact
    variant -- departure date, duration, board and departure-airport slug, e.g.
    `.../luna-holiday-complex-912903.html?od-2027-01-13,7-dni,HB,z-warszawy` --
    unlike the bare `/oferty/.../slug-id.html` URL `normalize_offer` otherwise
    constructs from slugs alone, which carries none of that and lets Wakacje.pl
    default to an unrelated variant once opened. One anchor per id, confirmed
    1:1 with the `listingOffers` JSON `id` field on the very same fetched page --
    reading it here adds no request.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: dict[int, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attr_map = dict(attrs)
        raw_id = attr_map.get("data-test-offer-id")
        href = attr_map.get("href")
        if not raw_id or not href or not raw_id.isdigit():
            return
        self.links.setdefault(int(raw_id), href)


def extract_offer_links(html: str) -> dict[int, str]:
    """Map each offer's numeric id to its real on-page detail-page href.

    Best-effort only: this is read from the same already-fetched listing HTML
    as `decode_next_data`/`extract_offers`, never a separate request. Absent or
    unmatched markup simply yields no entry for that id -- `normalize_offer`
    then falls back to its own reconstructed URL, exactly as it did before this
    map existed.
    """
    if len(html) > MAX_HTML_BYTES:
        raise ValueError("Wakacje.pl listing HTML exceeds size limit")
    parser = OfferLinkParser()
    parser.feed(html)
    return parser.links


def _safe_variant_href(href: str, offer_id: int) -> str | None:
    """Only trust a real on-page href that is same-origin and names this offer.

    Cheap defense-in-depth on top of the `data-test-offer-id` keying: the site
    itself is the origin of this string (never user input), but it is still
    untrusted HTML content, so it is never handed to a notifier unchecked.
    """
    if any(char.isspace() for char in href):
        return None
    if not href.startswith(f"{BASE}/oferty/"):
        return None
    if f"-{offer_id}.html" not in href.split("?", 1)[0]:
        return None
    return href


def decode_next_data(html: str) -> dict[str, object]:
    """Locate the single __NEXT_DATA__ script and return its decoded JSON object."""
    if len(html) > MAX_HTML_BYTES:
        raise ValueError("Wakacje.pl listing HTML exceeds size limit")
    parser = NextDataScript()
    parser.feed(html)
    if parser.scripts != 1:
        raise ValueError(
            "Missing or ambiguous Wakacje.pl __NEXT_DATA__ script; possible access challenge"
        )
    raw_text = "".join(parser.parts).strip()
    if not raw_text:
        raise ValueError("Empty Wakacje.pl __NEXT_DATA__ payload")
    try:
        data = json.loads(raw_text)
    except ValueError as exc:
        raise ValueError("Invalid Wakacje.pl __NEXT_DATA__ JSON payload") from exc
    return mapping(data)


def extract_offers(next_data: dict[str, object]) -> list[dict[str, object]]:
    """Return the raw offer records from the one `listingOffers` dehydrated query.

    Confirmed offline (RECONNAISSANCE.md sec 3, 11.3): the offer records live at
    `props.dehydratedState.queries[?].state.data.offers.data`, in the single query
    whose `queryKey[0] == "listingOffers"`. Exactly one such query is required; more
    than one is ambiguous (never guessed which is authoritative), and none means the
    page no longer carries this shape.
    """
    props = mapping(next_data.get("props"))
    dehydrated = props.get("dehydratedState")
    if not isinstance(dehydrated, dict):
        raise ValueError("Missing Wakacje.pl dehydratedState")
    queries = mapping(dehydrated).get("queries")
    if not isinstance(queries, list):
        raise ValueError("Missing Wakacje.pl dehydratedState queries")
    candidates = [
        mapping(q)
        for q in queries
        if isinstance(q, dict)
        and isinstance(q.get("queryKey"), list)
        and q["queryKey"][:1] == ["listingOffers"]
    ]
    if len(candidates) != 1:
        raise ValueError("Missing or ambiguous Wakacje.pl listingOffers query")
    state = mapping(candidates[0].get("state"))
    data = state.get("data")
    if not isinstance(data, dict):
        raise ValueError("Missing Wakacje.pl listingOffers data")
    offers_block = mapping(data).get("offers")
    if not isinstance(offers_block, dict):
        raise ValueError("Missing Wakacje.pl offers block")
    records = mapping(offers_block).get("data")
    if not isinstance(records, list):
        raise ValueError("Missing Wakacje.pl offers list")
    return [mapping(o) for o in records]


def _slug_or_none(value: str) -> str | None:
    return value if _SLUG.fullmatch(value) else None


def variant_identity(raw: RawOffer, departure_date: date) -> str:
    """A stable key across the confirmed variant-defining fields only.

    The numeric source `id` alone is not a stable variant identifier: the same id was
    observed, across two different departure-airport fetches, with a different
    departure date AND price (RECONNAISSANCE.md sec 12.1). Only fields whose
    variant-defining role is confirmed are included -- nothing with unconfirmed
    semantics (e.g. `departurePlaces`, `ratingReservationCount`) is added.
    """
    identity = {
        "source_id": raw.id,
        "departure_airport_code": raw.departurePlaceCode,
        "departure_date": departure_date.isoformat(),
        "duration": raw.duration,
        "service": raw.service,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def normalize_offer(
    raw_dict: dict[str, object],
    observed_at: datetime,
    *,
    requested_departure_airport: str | None = None,
    offer_links: dict[int, str] | None = None,
) -> Offer:
    """Normalize one already-specific Wakacje.pl listing card.

    `requested_departure_airport`, when given, is the IATA-style code of the
    single-flag filter used for this fetch (e.g. "WRO" for `?z-wroclawia`); every
    returned offer is required to match it exactly -- confirmed to always hold in
    reconnaissance (sec 12.1). A mismatch means the site's filtering contract
    changed and must fail loudly, not be silently accepted.

    `offer_links`, when given (from `extract_offer_links` on the same fetched
    HTML), maps this offer's numeric id to its real on-page href, which is
    preferred over the reconstructed slug URL below -- see `OfferLinkParser`.
    """
    raw = RawOffer.model_validate(raw_dict)
    if not _AIRPORT_CODE.fullmatch(raw.departurePlaceCode):
        raise ValueError("Unrecognized Wakacje.pl departure airport code")
    if (
        requested_departure_airport is not None
        and raw.departurePlaceCode != requested_departure_airport
    ):
        raise ValueError(
            "Wakacje.pl filtered listing returned an offer for an unexpected departure airport"
        )
    departure = date.fromisoformat(raw.departureDate)
    returning = date.fromisoformat(raw.returnDate)
    if returning <= departure:
        raise ValueError("Wakacje.pl return date does not follow departure date")
    if (returning - departure).days != raw.duration:
        raise ValueError("Wakacje.pl duration disagrees with departure/return dates")

    # `raw.price` is the site's own per-person figure, not a total: this
    # provider's one confirmed search query includes `za-osobe` ("average per
    # person"), the reverse of the site's plain default (`totalPrice: true,
    # pricePerPerson: false`). Confirmed directly, not inferred: live-fetched
    # offers' raw `price` values matched, PLN for PLN, the per-person figures
    # visibly labeled "średnia za osobę" on the real page during the same
    # manual session that confirmed this query (RECONNAISSANCE.md sec 25).
    price_per_person = Decimal(raw.price)
    total_price = price_per_person * Decimal(ASSUMED_PARTY_SIZE)

    country = COUNTRIES.get(raw.place.country.slug)
    places = [raw.place.region.name, raw.place.city.name]
    if country is None:
        places.insert(0, raw.place.country.name)
    destination = " / ".join(part for part in places if part) or None

    slugs = (
        _slug_or_none(raw.place.country.slug),
        _slug_or_none(raw.place.region.slug),
        _slug_or_none(raw.place.city.slug),
        _slug_or_none(raw.urlName),
    )
    url = (
        f"{BASE}/oferty/{slugs[0]}/{slugs[1]}/{slugs[2]}/{slugs[3]}-{raw.id}.html"
        if all(slugs)
        else None
    )
    real_href = (offer_links or {}).get(raw.id)
    if real_href is not None:
        validated_href = _safe_variant_href(real_href, raw.id)
        if validated_href is not None:
            url = validated_href

    rating = raw.ratingValue
    if rating is not None and not 0 <= rating <= 10:
        rating = None

    identity = variant_identity(raw, departure)

    return Offer(
        provider="wakacje.pl",
        offer_id=identity,
        variant_identity=identity,
        hotel_name=raw.name,
        country=country,
        destination=destination,
        departure_airport=raw.departurePlaceCode,
        departure_date=departure,
        return_date=returning,
        number_of_days=raw.duration,
        number_of_people=ASSUMED_PARTY_SIZE,
        price_per_person=price_per_person,
        total_price=total_price,
        currency=raw.originalCurrency,
        hotel_stars=raw.category,
        rating=rating,
        # ratingReservationCount's exact semantics are not literally confirmed
        # (RECONNAISSANCE.md sec 8b.1); never mapped to number_of_reviews to avoid
        # asserting an unconfirmed meaning.
        number_of_reviews=None,
        provider_rating_max=10.0,
        board_type=SERVICE_BOARDS.get(raw.service),
        url=url,
        found_at=observed_at,
        last_seen=observed_at,
        price_is_complete=False,
        variant_verified=False,
        price_notes=(
            f"Board detail: {raw.serviceDesc}; raw ratingReservationCount="
            f"{raw.ratingReservationCount} (semantics unconfirmed, not mapped to reviews); "
            f"per-person price (za-osobe), total computed for {ASSUMED_PARTY_SIZE} adults, "
            f"mandatory costs not verified"
        ),
        price_verification_reason=(
            "Listing-only observation; Wakacje.pl's detail page carries no offer-specific "
            "data for any robots-compliant fetch (RECONNAISSANCE.md sec 12.2), so booking-"
            "total confirmation is not currently possible"
        ),
    )


def parse_listing(
    html: str,
    observed_at: datetime,
    *,
    requested_departure_airport: str | None = None,
) -> list[Offer]:
    """Full offline pipeline: decode, extract, normalize; skip unreadable records."""
    next_data = decode_next_data(html)
    raw_offers = extract_offers(next_data)
    offer_links = extract_offer_links(html)
    result: list[Offer] = []
    for index, raw in enumerate(raw_offers):
        try:
            result.append(
                normalize_offer(
                    raw,
                    observed_at,
                    requested_departure_airport=requested_departure_airport,
                    offer_links=offer_links,
                )
            )
        except (ValidationError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Wakacje.pl record %s rejected: %s", index, exc)
    if raw_offers and not result:
        raise ValueError("No readable Wakacje.pl records; possible schema change")
    return result
