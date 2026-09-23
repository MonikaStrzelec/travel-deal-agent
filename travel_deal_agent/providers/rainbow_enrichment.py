"""Shortlist and map verified configurations without changing price eligibility."""

import hashlib
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

from ..boards import board_matches_price
from ..config_types import FilterConfig
from ..models import Offer
from ..ratings import rating_matches
from .rainbow_details import RainbowSelectedVariant
from .rainbow_listing_data import ListingEvidence
from .rainbow_nuxt import require


def potential_candidate(
    offer: Offer,
    filters: FilterConfig,
    today: date,
    evidence: ListingEvidence | None = None,
) -> bool:
    """A necessary-condition shortlist, never a fabricated eligible Offer."""
    if (
        offer.country is None
        or offer.departure_date is None
        or offer.number_of_days is None
        or offer.hotel_stars is None
        or offer.price_per_person is None
    ):
        return False
    if not (
        offer.currency == filters["currency"]
        and offer.number_of_people == filters["people"]
        and 0 < offer.price_per_person <= Decimal(filters["max_price"])
        and offer.departure_date >= today
        and offer.hotel_stars
        >= filters["country_min_stars"].get(offer.country, filters["min_stars"])
        and offer.number_of_days >= filters["min_days"]
        and (filters["max_days"] is None or offer.number_of_days <= filters["max_days"])
        and rating_matches(offer, filters["provider_ratings"])
    ):
        return False
    airports = (
        evidence.airports
        if evidence
        else (() if offer.departure_airport is None else (offer.departure_airport,))
    )
    boards = (
        evidence.boards if evidence else (() if offer.board_type is None else (offer.board_type,))
    )
    if airports and not any(a in filters["airports"] for a in airports):
        return False
    return not boards or any(
        b in filters["allowed_boards"]
        and board_matches_price(
            b,
            offer.price_per_person,
            filters["board_price_bands"],
        )
        for b in boards
    )


def enrich_selected(
    offer: Offer,
    evidence: ListingEvidence,
    variant: RainbowSelectedVariant,
) -> Offer:
    require(
        variant.product_key == evidence.product_key
        and variant.opaque_key == evidence.opaque_key
        and variant.price_per_person == evidence.price == offer.price_per_person
        and variant.departure_date == evidence.departure_date == offer.departure_date
        and variant.days == evidence.days == offer.number_of_days
        and variant.nights == evidence.nights
        and variant.outbound.departure_airport in evidence.airports
        and variant.board in evidence.boards
        and (
            offer.departure_airport is None
            or offer.departure_airport == variant.outbound.departure_airport
        )
        and (offer.board_type is None or offer.board_type == variant.board),
        "Detail configuration conflicts with listing evidence",
    )
    # Keep the existing observation ID/history; this identity is additional evidence only.
    identity = {
        "product": variant.product_key,
        "key": variant.opaque_key,
        "room": [variant.room.type_id, variant.room.configuration_id, variant.room.count],
        "airports": [
            variant.outbound.departure_airport,
            variant.outbound.arrival_airport,
            variant.inbound.departure_airport,
            variant.inbound.arrival_airport,
        ],
        "dates": [str(variant.departure_date), str(variant.return_date)],
        "party": [variant.adults, variant.children],
        "board": variant.board,
    }
    return replace(
        offer,
        departure_airport=variant.outbound.departure_airport,
        return_date=variant.return_date,
        board_type=variant.board,
        total_price=variant.total_price,
        url=evidence.url,
        variant_verified=True,
        variant_identity="rainbow-"
        + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
        price_is_complete=False,
        booking_total_price=None,
        price_verification_reason=variant.price_verification_reason,
        price_notes=f"{variant.provenance}; product={variant.product_key}; key={variant.opaque_key}; "
        f"room={variant.room.name} ({variant.room.type_id}/{variant.room.configuration_id}); "
        "selected calculator quote, not a confirmed complete booking price. "
        "Opaque key stability and current booking availability are unverified.",
    )
