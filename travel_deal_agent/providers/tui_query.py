"""Deterministic TUI search-URL builder for one confirmed manual-recording contract.

Every token here reproduces a fragment confirmed by one manual Playwright Codegen
recording of `/wypoczynek/wyniki-wyszukiwania-samolot?q=...`. Two things in that
reproduction are inferred, not directly confirmed, and must be checked against a
real response before being relied on for a live run:

- The single ':' join character and per-token repetition (":a:CODE" once per airport,
  "board:..." once per selected board group) are inferred from the fragments shown
  (e.g. ":byPlane:T" itself joins two tokens with ':'), not from the complete literal
  recorded string.
- `fullPrice=false` used '=' unlike every other ':'-joined fragment in the recording,
  so it is treated here as a separate top-level query parameter rather than part of
  `q`.

Duration (`dF:<min>:dT:<max>`) is deliberately NOT narrowed to the configured
`min_nights`/`max_nights` business range: the recording only showed TUI's own
unmodified default, `dF:6:dT:14` -- never a value someone actually changed. Whether
the backend accepts arbitrary dF/dT pairs, or only specific presets, was not
confirmed. `6-14` is used because it is the one positively confirmed encoding and a
superset of any configured range this module accepts (see `_duration_tokens`). The
shared `filtering.matches()` remains the sole authority that narrows results to the
configured nights range; nothing here claims to do that narrowing itself.

`tripAdvisorRating:4t` was present in the recording but is intentionally NOT applied
by default: the project's current business configuration has no minimum-rating
requirement for `tui` (no `filters["provider_ratings"]["tui"]` entry), and silently
adding one here would introduce a new, undiscussed hard filter (reject sub-4.0 hotels
before our own ranking ever sees them). If a future `provider_ratings.tui` rule is
enabled in configuration, its floor is honored; otherwise no threshold is sent.

`amountRange:%231500` is also NOT reproduced as-is: `amountRange` is labeled "Cena za
wszystkich" (total party price), a distinct facet from a separate per-person "Cena"
(`priceSelector`) facet. The 1500 typed during the manual recording was a mistake
(meant to be our per-person cap, but this field is the party total) -- not a
confirmed business value. This module instead computes the upstream `amountRange`
value as `filters["max_price"] * filters["people"]` (per-person cap times party
size), and never hardcodes it; the shared `filtering.matches()` remains the sole
authority that enforces the real per-person price cap on the normalized per-person
offer price.
"""

from collections.abc import Sequence
from decimal import Decimal
from urllib.parse import quote

from ..config_types import FilterConfig, HotelWatchlistEntry

PATH = "/wypoczynek/wyniki-wyszukiwania-samolot"

# Airport codes as exposed by the site's own airport dropdown; KTW/LCJ/WAW/WRO were
# independently reconfirmed in the Codegen recording.
KNOWN_AIRPORTS = frozenset(
    {"BZG", "GDN", "KTW", "KRK", "LCJ", "LUZ", "POZ", "RZE", "SZZ", "WAW", "WMI", "RDO", "WRO"}
)
# Disabled on TUI's own site (`"enabled": false` in the airport dropdown data).
# Silently excluded rather than rejected: a known, documented site limitation, not an
# unrecognized configuration value.
DISABLED_AIRPORTS = frozenset({"WMI"})

# One query token per selected board-type checkbox in the confirmed recording.
BOARD_FACETS = {
    "AI": "GT06-AI GT06-XX GT06-AIP",
    "FB": "GT06-FB GT06-FBP",
    "HB": "GT06-HB GT06-HBP",
}

STAR_CODES = {3: "3s", 4: "4s", 5: "5s"}

# TripAdvisor floor codes from the site's own rating facet definitions, used only
# if a future `provider_ratings.tui` rule enables one.
RATING_CODES = {
    Decimal("3"): "3t",
    Decimal("3.5"): "3.5t",
    Decimal("4"): "4t",
    Decimal("4.5"): "4.5t",
}

# The one confirmed duration encoding: TUI's own unmodified default, "6-14" nights.
# See the module docstring for why 7-9 is not encoded directly.
DURATION_FROM, DURATION_TO = 6, 14


def _airports(filters: FilterConfig) -> list[str]:
    selected = [a for a in filters["airports"] if a not in DISABLED_AIRPORTS]
    unknown = [a for a in selected if a not in KNOWN_AIRPORTS]
    if unknown:
        raise ValueError(f"Unsupported TUI airport code(s): {unknown}")
    if not selected:
        raise ValueError("No TUI-enabled airport remains after excluding disabled codes")
    return selected


def _boards(filters: FilterConfig) -> list[str]:
    """Search only the confirmed subset of `allowed_boards` TUI actually has a facet for.

    `allowed_boards` is a cross-provider business whitelist (e.g. it now also
    includes "ZO", a Wakacje.pl-only board with no TUI equivalent). A board this
    provider cannot search for is simply never returned by TUI anyway, so
    dropping it here loses no filtering integrity -- `filtering.matches()`
    still enforces the full whitelist on whatever TUI actually returns. This
    only fails when NONE of the allowed boards have a confirmed TUI facet,
    since that would silently search for nothing.
    """
    boards = filters["allowed_boards"]
    if not boards:
        raise ValueError("No allowed board configured")
    confirmed = [b for b in boards if b in BOARD_FACETS]
    if not confirmed:
        raise ValueError(f"No confirmed TUI facet for any allowed board: {boards}")
    return [BOARD_FACETS[b] for b in confirmed]


def _star_code(filters: FilterConfig) -> str:
    minimum = filters["min_stars"]
    code = STAR_CODES.get(int(minimum)) if float(minimum).is_integer() else None
    if code is None:
        raise ValueError("TUI only supports minHotelCategory thresholds of 3, 4 or 5 stars")
    return code


def _party(filters: FilterConfig) -> tuple[str, str]:
    if filters["people"] != 2:
        raise ValueError("TUI POC currently supports exactly two adults, zero children")
    return "2", "0"


def _price_ceiling(filters: FilterConfig) -> str:
    """`amountRange` is TUI's "Cena za wszystkich" (total party price) facet, a
    distinct filter from the separate per-person `priceSelector` facet.
    `filters["max_price"]` is our business cap PER PERSON, so the upstream value
    must be `max_price * people`, not passed through unchanged (see the module
    docstring).
    """
    per_person = Decimal(filters["max_price"])
    if not per_person.is_finite() or per_person <= 0:
        raise ValueError("Invalid TUI price cap")
    people = filters["people"]
    if type(people) is not int or people <= 0:
        raise ValueError("Invalid TUI party size")
    return "#" + format(per_person * people, "f")


def _duration_tokens(filters: FilterConfig) -> tuple[str, str]:
    """`None` means no configured bound on that side and is always covered."""
    minimum, maximum = filters["min_nights"], filters["max_nights"]
    if (minimum is not None and minimum < DURATION_FROM) or (
        maximum is not None and maximum > DURATION_TO
    ):
        raise ValueError(
            f"Configured stay length [{minimum}, {maximum}] is not covered by the one "
            f"confirmed TUI duration encoding [{DURATION_FROM}, {DURATION_TO}]"
        )
    return str(DURATION_FROM), str(DURATION_TO)


def _rating_token(filters: FilterConfig) -> str | None:
    """Only used if a future `provider_ratings.tui` rule is explicitly enabled."""
    rule = filters["provider_ratings"].get("tui")
    if not rule or not rule["enabled"] or not rule["price_bands"]:
        return None
    minimum = Decimal(str(min(band["min_rating"] for band in rule["price_bands"])))
    candidates = sorted(code for code in RATING_CODES if code <= minimum)
    if not candidates:
        raise ValueError("Configured tui rating floor is below any confirmed TripAdvisor threshold")
    return RATING_CODES[candidates[-1]]


def build_search_path(filters: FilterConfig, *, page: int = 1) -> str:
    """Build the confirmed `/wypoczynek/wyniki-wyszukiwania-samolot?q=...` path.

    Derives every value from the shared `FilterConfig`; no business threshold is
    hardcoded here beyond the fixed protocol tokens (`byPlane`, `tripType`) and the
    one confirmed duration encoding documented above.

    `page` is 1-indexed, matching TUI's own Next.js data-prefetch URL for this
    route, which defaults to `&page=1` when the browser URL carries no explicit
    page. See `tui.py`'s module docstring for how the provider bounds the number
    of pages it actually requests.
    """
    if filters["currency"] != "PLN":
        raise ValueError("TUI POC currently supports PLN only")
    if page < 1:
        raise ValueError("TUI page must be a positive integer")
    adults, children = _party(filters)
    duration_from, duration_to = _duration_tokens(filters)

    tokens: list[str] = ["price", "byPlane", "T"]
    for airport in _airports(filters):
        tokens += ["a", airport]
    tokens += ["ctAdult", adults, "ctChild", children]
    tokens += ["amountRange", _price_ceiling(filters)]
    for group in _boards(filters):
        tokens += ["board", group]
    tokens += ["minHotelCategory", _star_code(filters)]
    rating = _rating_token(filters)
    if rating is not None:
        tokens += ["tripAdvisorRating", rating]
    tokens += ["tripType", "WS"]
    tokens += ["dF", duration_from, "dT", duration_to]

    q = ":" + ":".join(tokens)
    suffix = f"&page={page}" if page != 1 else ""
    return f"{PATH}?q={quote(q, safe='')}&fullPrice=false{suffix}"


# --- Hotel watchlist: a second, independent search query -----------------------
#
# `filters["max_price"]` (via `_price_ceiling` above) is exactly what keeps
# `build_search_path`'s own `amountRange` facet -- sent straight to TUI's own
# search API -- from ever returning a watched hotel priced above the standard
# 1500 PLN/person cap: this is a query-level block, not merely a client-side
# filter, so a watched hotel above that cap can never appear in `fetch()`'s own
# results at all. `build_watchlist_search_path` below is a second, independent
# query built the same way but never derived from `filters`, so a watched
# hotel's own (higher) price ceiling reaches TUI's own search instead. See
# `tui.py`'s `fetch_watchlist_offers` for how this is used -- entirely separate
# from `fetch()`, one extra listing request plus up to `max_detail_requests`
# confirmations, matching ITAKA's/wakacje.pl's own separate, small watchlist
# budget.


def _watchlist_price_ceiling(entries: Sequence[HotelWatchlistEntry]) -> str:
    """The widest configured two-adult watchlist cap, scaled like `_price_ceiling`.

    Never derived from `filters["max_price"]`: a watched hotel has its own,
    independent price ceiling (`config_types.HotelWatchlistEntry`) that must
    never be narrowed by the standard search's cap. Entries for a party size
    other than two adults are excluded here (same POC limitation as `_party`
    above) rather than raising, so one unsupported entry never blocks a search
    for the others.
    """
    ceilings = [Decimal(entry["max_price_per_person"]) for entry in entries if entry["people"] == 2]
    if not ceilings:
        raise ValueError("No two-adult hotel_watchlist entry to search TUI for")
    return "#" + format(max(ceilings) * 2, "f")


PROVIDER_NAME = "tui"


def _destination(entry: HotelWatchlistEntry) -> str | None:
    return entry.get("provider_destinations", {}).get(PROVIDER_NAME)


def split_watchlist_entries(
    entries: Sequence[HotelWatchlistEntry],
) -> tuple[list[HotelWatchlistEntry], list[HotelWatchlistEntry]]:
    """Two-adult entries with a configured TUI destination code, and those without.

    Kept apart because TUI's `c:<code>` facet is a hard filter: one query mixing
    both kinds would silently hide every unscoped entry's hotel.
    """
    supported = [entry for entry in entries if entry["people"] == 2]
    scoped = [entry for entry in supported if _destination(entry) is not None]
    return scoped, [entry for entry in supported if _destination(entry) is None]


def _watchlist_destinations(entries: Sequence[HotelWatchlistEntry]) -> list[str]:
    """Sorted, de-duplicated destination codes; empty unless EVERY entry has one."""
    codes = [_destination(entry) for entry in entries]
    if not codes or any(code is None for code in codes):
        return []
    return sorted({code for code in codes if code is not None})


def _watchlist_airports(entries: Sequence[HotelWatchlistEntry]) -> list[str]:
    """The union of every (two-adult) watched hotel's own configured airports."""
    codes = sorted({airport for entry in entries for airport in entry["airports"]})
    selected = [a for a in codes if a not in DISABLED_AIRPORTS]
    unknown = [a for a in selected if a not in KNOWN_AIRPORTS]
    if unknown:
        raise ValueError(f"Unsupported TUI airport code(s): {unknown}")
    if not selected:
        raise ValueError("No TUI-enabled airport remains after excluding disabled codes")
    return selected


def build_watchlist_search_path(entries: Sequence[HotelWatchlistEntry], *, page: int = 1) -> str:
    """Build a second, independent TUI search query for the hotel watchlist only.

    Entirely separate from `build_search_path`: never derives its price
    ceiling, board or star facets from `filters` -- a watched hotel is never
    subject to `filters["max_price"]` or the board/rating/star rules (see
    `config_types.HotelWatchlistEntry`). Board and star facets are
    deliberately loosened to every confirmed option (all of `BOARD_FACETS`,
    the lowest confirmed `minHotelCategory` code) rather than the standard
    search's own `allowed_boards`/`min_stars`: narrowing either here could
    otherwise hide the watched hotel's own actual board/star listing from this
    query's results before `watchlist.matches()` (which checks neither) ever
    gets a chance to evaluate it. Duration reuses the same confirmed,
    unrestricted `dF:6:dT:14` default as `build_search_path` -- still a
    superset of every watchlist entry's own `min_nights`/`max_nights` in
    production today.

    `c:<code>` destination tokens (confirmed live 2026-09-30: the site's own
    destination picker sends one per destination code, e.g. `c:HRG`) are added
    only when EVERY given entry configures `provider_destinations["tui"]`;
    callers pass scoped and unscoped entries separately (see
    `split_watchlist_entries`). A destination-scoped query is typically one
    page, versus dozens for the global price-sorted listing.

    Only entries usable by this POC (`entry["people"] == 2`) are considered;
    an empty result after that filter raises (see `_watchlist_price_ceiling`),
    since there is then nothing left to search TUI for.
    """
    if page < 1:
        raise ValueError("TUI page must be a positive integer")
    tokens: list[str] = ["price", "byPlane", "T"]
    for airport in _watchlist_airports(entries):
        tokens += ["a", airport]
    tokens += ["ctAdult", "2", "ctChild", "0"]
    for destination in _watchlist_destinations(entries):
        tokens += ["c", destination]
    tokens += ["amountRange", _watchlist_price_ceiling(entries)]
    for group in BOARD_FACETS.values():
        tokens += ["board", group]
    tokens += ["minHotelCategory", STAR_CODES[min(STAR_CODES)]]
    tokens += ["tripType", "WS"]
    tokens += ["dF", str(DURATION_FROM), "dT", str(DURATION_TO)]

    q = ":" + ":".join(tokens)
    suffix = f"&page={page}" if page != 1 else ""
    return f"{PATH}?q={quote(q, safe='')}&fullPrice=false{suffix}"
