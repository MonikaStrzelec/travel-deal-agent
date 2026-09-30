"""Independent hotel-watchlist matching, entirely separate from `filtering.py`.

A watched hotel is a specific property chosen explicitly --
unlike the standard search, it is never subject to `filters["max_price"]` or
the board/rating/star rules (see `config_types.HotelWatchlistEntry`). Adding
another watched hotel is a config-only change: nothing here hardcodes any
particular hotel's name, country or price.

## Name matching

Provider listings are not expected to spell a hotel's name identically, but a
similar-looking name must not be confused with a different hotel. This module
therefore uses simple, predictable matching -- configured aliases plus
normalization (diacritics, case and punctuation stripped, whitespace
collapsed) -- never fuzzy/approximate string matching. A structurally
different spelling of the brand name (e.g. a two-word vs. one-word brand
prefix) is not covered by normalization and must be listed as its own alias.
"""

import logging
import re
import unicodedata
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from .config_types import HotelWatchlistEntry
from .models import Offer

logger = logging.getLogger(__name__)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_hotel_name(name: str) -> str:
    """Casefold, strip diacritics/punctuation, collapse whitespace.

    Deliberately simple and predictable (see the module docstring): this
    never reorders words, drops substrings or tolerates missing words --
    it only absorbs spelling noise (accents, hyphens vs. spaces, case).
    """
    decomposed = unicodedata.normalize("NFKD", name)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(_NON_ALNUM.sub(" ", without_accents.casefold()).split())


def _aliases(entry: HotelWatchlistEntry) -> tuple[str, ...]:
    """The configured name is always an accepted alias, whether or not it is
    also repeated inside `aliases`."""
    return (entry["name"], *entry["aliases"])


def hotel_identity_matches(offer: Offer, entry: HotelWatchlistEntry) -> bool:
    """Name (via configured aliases) plus an optional country safeguard.

    Missing offer data never confirms identity (no name -> no match), but
    missing `country` data on the offer is not treated as a mismatch --
    country is an extra safeguard against a different hotel with a similar
    name, not a required field every provider is expected to supply.
    """
    if offer.hotel_name is None:
        return False
    normalized = normalize_hotel_name(offer.hotel_name)
    if not any(normalized == normalize_hotel_name(alias) for alias in _aliases(entry)):
        return False
    country = entry.get("country")
    return country is None or offer.country is None or offer.country == country


def _rejection_reason(
    offer: Offer,
    entry: HotelWatchlistEntry,
    accept_incomplete_price_from: Sequence[str],
    today: date | None,
) -> str | None:
    """Why an identity-matched offer fails this entry's own rules, or None when
    it qualifies. The single source of truth for `matches`."""
    if not (offer.price_is_complete or offer.provider in accept_incomplete_price_from):
        return "price not confirmed for this provider"
    if (
        offer.departure_date is None
        or offer.return_date is None
        or offer.price_per_person is None
        or offer.number_of_people is None
        or offer.departure_airport is None
    ):
        return "incomplete offer data"
    if offer.number_of_people != entry["people"]:
        return f"party of {offer.number_of_people}, expected {entry['people']}"
    limit = Decimal(entry["max_price_per_person"])
    if not Decimal("0") < offer.price_per_person <= limit:
        return f"price {offer.price_per_person}/person outside (0, {limit}]"
    if offer.departure_airport not in entry["airports"]:
        return f"airport {offer.departure_airport} not watched"
    nights = (offer.return_date - offer.departure_date).days
    minimum_nights = entry.get("min_nights")
    maximum_nights = entry.get("max_nights")
    if minimum_nights is not None and nights < minimum_nights:
        return f"{nights} nights below minimum {minimum_nights}"
    if maximum_nights is not None and nights > maximum_nights:
        return f"{nights} nights above maximum {maximum_nights}"
    if offer.departure_date < (today or date.today()):
        return "departure date in the past"
    return None


def matches(
    offer: Offer,
    entry: HotelWatchlistEntry,
    accept_incomplete_price_from: Sequence[str],
    today: date | None = None,
) -> bool:
    """One watched hotel's own eligibility rule: identity, price ceiling, stay
    length, airport and party size -- deliberately not the board/rating/star
    rules `filtering.matches()` enforces for the standard search.

    `accept_incomplete_price_from` mirrors the same per-provider whitelist
    `filtering.matches()` uses (`filters["accept_incomplete_price_from"]`) --
    a single, shared business decision about which providers' listing prices
    are trustworthy, not a second, driftable copy of it.

    An offer of the watched hotel that fails a rule is logged with its reason,
    so an unattended run shows why a candidate was not alerted.
    """
    if not hotel_identity_matches(offer, entry):
        return False
    reason = _rejection_reason(offer, entry, accept_incomplete_price_from, today)
    if reason is not None:
        logger.info(
            "Watchlist candidate %r from %s rejected: %s", entry["name"], offer.provider, reason
        )
    return reason is None


def matches_any(
    offer: Offer,
    entries: Sequence[HotelWatchlistEntry],
    accept_incomplete_price_from: Sequence[str],
    today: date | None = None,
) -> HotelWatchlistEntry | None:
    """The first watchlist entry this offer qualifies for, or None.

    An offer can only ever be matched to one entry's notification per cycle
    (see `pipeline.OfferPipeline.filter_watchlist_batch`); with today's single
    configured hotel this is moot, but stays well-defined once a second entry
    is added.
    """
    for entry in entries:
        if matches(offer, entry, accept_incomplete_price_from, today):
            return entry
    return None
