"""Destination-scoped ITAKA watchlist listing URL.

Confirmed live 2026-09-30: ITAKA's own server-rendered search accepts a country
slug in the path, e.g. `/wyniki-wyszukiwania/wakacje/<slug>/`, together with the
same query parameters the site's own form produces (`departuresByPlane`,
`durationMin`, `adults[0]`, `order=priceAsc`, `priceTo` = per-person cap). The page
carries the same `__NEXT_DATA__` payload `itaka_data.parse_page` already reads.

The slug comes from `HotelWatchlistEntry.provider_destinations["itaka"]` and is
never derived or guessed here. Nothing in this module names a hotel or country.
"""

import re
from collections.abc import Sequence
from decimal import Decimal
from urllib.parse import quote

from ..config_types import HotelWatchlistEntry

PROVIDER_NAME = "itaka"
PATH = "/wyniki-wyszukiwania/wakacje/"
SLUG = re.compile(r"[a-z0-9-]+")
_AIRPORT = re.compile(r"[A-Z]{3}")


def destination_groups(
    entries: Sequence[HotelWatchlistEntry],
) -> dict[str, list[HotelWatchlistEntry]]:
    """Two-adult entries with a configured ITAKA destination slug, grouped by slug
    (first-seen order). `adults[0]=2` is fixed, like the standard search."""
    groups: dict[str, list[HotelWatchlistEntry]] = {}
    for entry in entries:
        slug = entry.get("provider_destinations", {}).get(PROVIDER_NAME)
        if entry["people"] == 2 and slug is not None:
            groups.setdefault(slug, []).append(entry)
    return groups


def build_watchlist_path(
    slug: str, entries: Sequence[HotelWatchlistEntry], *, page: int = 1
) -> str:
    """Path plus query for one destination: the union of the group's airports, the
    widest per-person cap and the loosest stay-length floor (no floor if any
    entry has none). `watchlist.matches()` still applies each entry's own rules."""
    if not SLUG.fullmatch(slug):
        raise ValueError(f"Invalid ITAKA destination slug: {slug!r}")
    if not entries:
        raise ValueError("No hotel_watchlist entry to search ITAKA for")
    if page < 1:
        raise ValueError("ITAKA page must be a positive integer")
    airports = sorted({airport for entry in entries for airport in entry["airports"]})
    if not airports or any(not _AIRPORT.fullmatch(airport) for airport in airports):
        raise ValueError(f"Unsupported ITAKA airport code(s): {airports}")
    ceiling = max(Decimal(entry["max_price_per_person"]) for entry in entries)
    minimums = [entry.get("min_nights") for entry in entries]
    params = [("departuresByPlane", ",".join(airports))]
    if all(minimum is not None for minimum in minimums):
        params.append(("durationMin", str(min(m for m in minimums if m is not None))))
    params += [("adults[0]", "2"), ("order", "priceAsc"), ("priceTo", format(ceiling, "f"))]
    if page > 1:
        params.append(("page", str(page)))
    query = "&".join(f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in params)
    return f"{PATH}{slug}/?{query}"
