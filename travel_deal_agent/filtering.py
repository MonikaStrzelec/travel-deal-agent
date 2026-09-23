"""Hard filters reject missing data needed to verify eligibility."""

from datetime import date
from decimal import Decimal

from .boards import board_matches_price, normalize_board
from .config_types import FilterConfig
from .models import Offer
from .ratings import rating_matches


def matches(offer: Offer, filters: FilterConfig, today: date | None = None) -> bool:
    """Require enough data to verify every configured hard filter.

    A price left incomplete by its source (`Offer.price_is_complete=False`) is
    still eligible when the offer's own provider is explicitly opted in via
    `filters["accept_incomplete_price_from"]` -- a per-provider business
    decision, not a global weakening. Providers absent from that list (the
    default for all of them) keep the original, unconditional requirement.
    """
    price_ok = offer.price_is_complete or offer.provider in filters.get(
        "accept_incomplete_price_from", []
    )
    return price_ok and matches_criteria(offer, filters, today)


def matches_criteria(offer: Offer, filters: FilterConfig, today: date | None = None) -> bool:
    """Shared criteria; a listing shortlist does not establish booking price completeness."""
    if (
        offer.country is None
        or offer.departure_date is None
        or offer.return_date is None
        or offer.number_of_days is None
        or offer.hotel_stars is None
        or offer.price_per_person is None
    ):
        return False
    board = normalize_board(offer.provider, offer.board_type)
    maximum_days = filters["max_days"]
    minimum = filters["country_min_stars"].get(offer.country, filters["min_stars"])
    return (
        offer.currency == filters["currency"]
        and board in filters["allowed_boards"]
        and board_matches_price(board, offer.price_per_person, filters["board_price_bands"])
        and offer.number_of_people == filters["people"]
        and Decimal("0") < offer.price_per_person <= Decimal(str(filters["max_price"]))
        and offer.departure_airport in filters["airports"]
        and len(offer.country) == 2
        and offer.country.isupper()
        and offer.hotel_stars >= minimum
        and filters["min_days"] <= offer.number_of_days
        and (maximum_days is None or offer.number_of_days <= maximum_days)
        and offer.departure_date >= (today or date.today())
        and rating_matches(offer, filters["provider_ratings"])
    )
