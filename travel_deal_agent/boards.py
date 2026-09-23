"""Conservative meal normalization; provider codes are never globally guessed."""

import unicodedata
from decimal import Decimal

from .config_types import BoardPriceBand

BOARD_ORDER = ("RO", "BB", "HB", "FB", "AI", "UAI")
CANONICAL_BOARDS = frozenset(BOARD_ORDER)


def normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().replace("_", " ").split())


_NAMES = {
    "RO": ("ro", "room only", "self catering", "bez wyżywienia", "własne wyżywienie", "własne"),
    "BB": ("bb", "breakfast", "bed & breakfast", "bed and breakfast", "śniadanie", "śniadania"),
    "HB": (
        "hb",
        "half board",
        "śniadanie i obiadokolacja",
        "śniadania i obiadokolacje",
        "2 posiłki",
        "dwa posiłki",
        # Confirmed live (TUI, GT06-HBP, 2026-09-22 reconnaissance).
        "dwa posiłki plus",
    ),
    "FB": ("fb", "full board", "3 posiłki", "trzy posiłki"),
    "AI": ("ai", "all inclusive", "all inclusive 24h"),
    "UAI": ("uai", "ultra all inclusive", "all inclusive ultra", "all inclusive ultra 24h"),
}
_ALIASES = {name: board for board, names in _NAMES.items() for name in names}
# Only codes observed in each source's own listing are accepted. The title must agree.
ITAKA_CODES = {"H": "HB", "A": "AI"}
# Observed TUI board facet codes (source: TUI listing page facet definitions).
TUI_CODES = {
    "GT06-AI": "AI",
    "GT06-XX": "AI",
    "GT06-AIP": "AI",
    "GT06-FB": "FB",
    "GT06-FBP": "FB",
    "GT06-HB": "HB",
    "GT06-HBP": "HB",
    "GT06-BB": "BB",
    "GT06-AO": "RO",
}
_PROVIDER_CODES = {"itaka": ITAKA_CODES, "tui": TUI_CODES}


def normalize_board(provider: str, title: str | None, code: str | None = None) -> str | None:
    """Unknown or contradictory provider data does not prove breakfast is included."""
    board = _ALIASES.get(normalized_text(title)) if title else None
    if code is not None:
        known = _PROVIDER_CODES.get(provider, {}).get(code)
        if board is None or (known is not None and board != known):
            return None
        # An unrecognized code can be accompanied by an unambiguous full meal name.
        if known is None and normalized_text(title or "") in {"ro", "bb", "hb", "fb", "ai", "uai"}:
            return None
    return board


def board_matches_price(
    board: str | None, final_price: Decimal, bands: list[BoardPriceBand]
) -> bool:
    """Match canonical meals against final-price bands; gaps and ambiguity fail closed."""
    if board not in CANONICAL_BOARDS or board == "RO":
        return False
    applicable = [
        band
        for band in bands
        if Decimal(band["min_price"]) <= final_price
        and (
            final_price < Decimal(band["max_price"])
            or (band.get("max_inclusive", False) and final_price == Decimal(band["max_price"]))
        )
    ]
    if len(applicable) != 1:
        return False
    minimum = applicable[0]["min_board"]
    return (
        board is not None
        and minimum in BOARD_ORDER
        and BOARD_ORDER.index(board) >= BOARD_ORDER.index(minimum)
    )
