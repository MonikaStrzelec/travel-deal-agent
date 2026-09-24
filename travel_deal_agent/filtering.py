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
    """Shared criteria; a listing shortlist does not establish booking price completeness.

    No business rule depends on the destination country: every country the
    source returns is eligible under the same thresholds. A country that is
    present must still be a normalized two-letter code, but an unmapped
    (`None`) country is not a reason to reject an offer.
    """
    if (
        offer.departure_date is None
        or offer.return_date is None
        or offer.number_of_days is None
        or offer.hotel_stars is None
        or offer.price_per_person is None
    ):
        return False
    board = normalize_board(offer.provider, offer.board_type)
    # Canonical stay length for filtering: nights = return_date - departure_date.
    # This is the one duration signal that means the same thing for every
    # provider, unlike `Offer.number_of_days` (see the module docstring for
    # `notification_content._stay_length`, which explains the same split).
    nights = (offer.return_date - offer.departure_date).days
    minimum_nights = filters["min_nights"]
    maximum_nights = filters["max_nights"]
    return (
        offer.currency == filters["currency"]
        and board in filters["allowed_boards"]
        and board_matches_price(board, offer.price_per_person, filters["board_price_bands"])
        and offer.number_of_people == filters["people"]
        and Decimal("0") < offer.price_per_person <= Decimal(str(filters["max_price"]))
        and offer.departure_airport in filters["airports"]
        and (offer.country is None or (len(offer.country) == 2 and offer.country.isupper()))
        and offer.hotel_stars >= filters["min_stars"]
        and (minimum_nights is None or nights >= minimum_nights)
        and (maximum_nights is None or nights <= maximum_nights)
        and offer.departure_date >= (today or date.today())
        and rating_matches(offer, filters["provider_ratings"])
    )
