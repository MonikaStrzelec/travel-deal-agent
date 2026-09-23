"""Deterministic TUI search-URL builder for one confirmed manual-recording contract.

Every token here reproduces a fragment explicitly confirmed by one manual Playwright
Codegen recording of `/wypoczynek/wyniki-wyszukiwania-samolot?q=...`. Two things in
that reproduction are inferred, not directly confirmed, and must be checked against
a real response before being relied on for a live run:

- The single ':' join character and per-token repetition (":a:CODE" once per airport,
  "board:..." once per selected board group) are inferred from the fragments shown
  (e.g. ":byPlane:T" itself joins two tokens with ':'), not from the complete literal
  recorded string.
- `fullPrice=false` used '=' unlike every other ':'-joined fragment in the recording,
  so it is treated here as a separate top-level query parameter rather than part of
  `q`.

Duration (`dF:<min>:dT:<max>`) is deliberately NOT narrowed to the business target of
7-9 nights: the recording only showed TUI's own unmodified default, `dF:6:dT:14` (the
same "6-14" preset already seen selected by default in prior reconnaissance of the
duration facet) -- never a value someone actually changed. Whether the backend accepts
arbitrary dF/dT pairs, or only specific presets, was not confirmed offline. `6-14` is
used because it is the one positively confirmed encoding and a strict superset of
[7, 9]. The shared `filtering.matches()` remains the sole authority that narrows
results to exactly 7-9 nights; nothing here claims to do that narrowing itself.

`tripAdvisorRating:4t` was present in the recording but is intentionally NOT applied
by default: the project's current business configuration has no minimum-rating
requirement for `tui` (no `filters["provider_ratings"]["tui"]` entry), and silently
adding one here would introduce a new, undiscussed hard filter (reject sub-4.0 hotels
before our own ranking ever sees them). If a future `provider_ratings.tui` rule is
enabled in configuration, its floor is honored; otherwise no threshold is sent.

`amountRange:%231500` is also NOT reproduced as-is: reconnaissance found `amountRange`
labeled "Cena za wszystkich" (total party price), a distinct facet from a separate
per-person "Cena" (`priceSelector`) facet. The 1500 typed during the manual recording
was confirmed by the person recording it to be a mistake (they meant to enter our
per-person cap, but this field is the party total) -- not a confirmed business value.
This module instead computes the upstream `amountRange` value as
`filters["max_price"] * filters["people"]` (per-person cap times party size), and
never hardcodes 3000; the shared `filtering.matches()` remains the sole authority that
enforces the real PLN 1500/person cap on the normalized per-person offer price.
"""

from decimal import Decimal
from urllib.parse import quote

from ..config_types import FilterConfig

PATH = "/wypoczynek/wyniki-wyszukiwania-samolot"

# Confirmed via the airport dropdown's `availableAirports` data (prior reconnaissance);
# KTW/LCJ/WAW/WRO were independently confirmed again in the Codegen recording.
KNOWN_AIRPORTS = frozenset(
    {"BZG", "GDN", "KTW", "KRK", "LCJ", "LUZ", "POZ", "RZE", "SZZ", "WAW", "WMI", "RDO", "WRO"}
)
# Confirmed disabled on TUI's own site (reconnaissance found `"enabled": false`).
# Silently excluded rather than rejected: a known, documented site limitation, not an
# unrecognized configuration value.
DISABLED_AIRPORTS = frozenset({"WMI"})

# Confirmed board facet groups: one query token per selected checkbox in the recording.
BOARD_FACETS = {
    "AI": "GT06-AI GT06-XX GT06-AIP",
    "FB": "GT06-FB GT06-FBP",
    "HB": "GT06-HB GT06-HBP",
}

STAR_CODES = {3: "3s", 4: "4s", 5: "5s"}

# TripAdvisor floor codes observed in the duration/rating facet definitions
# (reconnaissance), used only if a future `provider_ratings.tui` rule enables one.
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
    boards = filters["allowed_boards"]
    if not boards:
        raise ValueError("No allowed board configured")
    unmapped = [b for b in boards if b not in BOARD_FACETS]
    if unmapped:
        raise ValueError(f"No confirmed TUI facet for board(s): {unmapped}")
    return [BOARD_FACETS[b] for b in boards]


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
    """`amountRange` is TUI's "Cena za wszystkich" (total party price) facet, confirmed
    during reconnaissance as a distinct filter from the separate per-person `priceSelector`
    facet. `filters["max_price"]` is our business cap PER PERSON, so the upstream value
    must be `max_price * people`, not passed through unchanged. (An earlier manual
    recording entered 1500 directly into this field; that was a recording mistake, not
    a confirmed business value, and must not be treated as authoritative.)
    """
    per_person = Decimal(filters["max_price"])
    if not per_person.is_finite() or per_person <= 0:
        raise ValueError("Invalid TUI price cap")
    people = filters["people"]
    if type(people) is not int or people <= 0:
        raise ValueError("Invalid TUI party size")
    return "#" + format(per_person * people, "f")


def _duration_tokens(filters: FilterConfig) -> tuple[str, str]:
    minimum, maximum = filters["min_days"], filters["max_days"]
    if minimum < DURATION_FROM or maximum is None or maximum > DURATION_TO:
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

    `page` (PAGINATION_LIVE_CONFIRMATION_NEEDED -- see `CURRENT_STATE.md`):
    every real listing captured so far had `pagination.pagesCount == 1` (13
    results fit in one `pageSize: 20` page), so a second page has never actually
    been requested or observed. The query-parameter *name* `page` is not a
    guess -- reconnaissance captured TUI's own Next.js data-prefetch URL for
    this exact route defaulting to `&page=1` when the browser URL carries no
    explicit page
    (`_next/data/<buildId>/wypoczynek/wyniki-wyszukiwania-samolot.json?...
    &page=1&...`). Whether appending `&page=2` here actually makes the site
    render, and passively expose through `search/offers`, a second and
    genuinely different page of results has never been confirmed live and is
    not assumed; `tui.py`'s provider does not call this with `page != 1`.
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
