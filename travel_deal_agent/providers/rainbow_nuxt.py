"""Bounded, non-executing reader for the observed Nuxt reference-table format."""

import json
import re
from datetime import datetime
from decimal import Decimal, DecimalException
from html.parser import HTMLParser
from typing import cast

from .rainbow_errors import RainbowStructureError

MAX_HTML_BYTES = 2_000_000
MAX_TABLE_ENTRIES = 20_000
MAX_REFERENCES = 50_000
MAX_DEPTH = 64
WRAPPERS = {"Ref", "ShallowRef", "Reactive", "ShallowReactive"}


class RainbowDetailError(RainbowStructureError):
    """Missing, ambiguous, inconsistent or unsupported saved detail evidence."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RainbowDetailError(message)


def mapping(value: object) -> dict[str, object]:
    require(isinstance(value, dict), "Expected detail object")
    return cast(dict[str, object], value)


def sequence(value: object) -> list[object]:
    require(isinstance(value, list), "Expected detail array")
    return cast(list[object], value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON field")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise RainbowDetailError("Nonfinite JSON number")


class _PayloadHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.payloads: list[str] = []
        self.active = False
        self.closed = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and ("id", "__NUXT_DATA__") in attrs:
            require(len(dict(attrs)) == len(attrs), "Duplicate script attribute")
            self.payloads.append("")
            self.active = True
            self.closed = False

    def handle_data(self, data: str) -> None:
        if self.active:
            self.payloads[-1] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.active:
            self.active = False
            self.closed = True


class NuxtTable:
    """Navigate named paths lazily; decode only requested evidence subtrees.

    Unrelated application state is deliberately not interpreted. Each consumed
    reference is checked, and decoded subtrees reject cycles and unknown tags.
    """

    def __init__(self, html: str) -> None:
        require(len(html) <= MAX_HTML_BYTES, "Detail HTML exceeds size limit")
        try:
            require(len(html.encode("utf-8")) <= MAX_HTML_BYTES, "Detail HTML exceeds size limit")
        except UnicodeError as exc:
            raise RainbowDetailError("Invalid HTML encoding") from exc
        parser = _PayloadHTML()
        parser.feed(html)
        parser.close()
        require(len(parser.payloads) == 1 and parser.closed, "Missing or ambiguous Nuxt payload")
        try:
            raw: object = json.loads(
                parser.payloads[0],
                parse_float=Decimal,
                parse_constant=_constant,
                object_pairs_hook=_unique_object,
            )
        except (ValueError, RecursionError, DecimalException) as exc:
            raise RainbowDetailError("Invalid Nuxt JSON") from exc
        self.table = sequence(raw)
        require(0 < len(self.table) <= MAX_TABLE_ENTRIES, "Invalid Nuxt table size")
        self.remaining = MAX_REFERENCES
        self.cache: dict[int, object] = {}

    def _index(self, ref: object) -> int:
        self.remaining -= 1
        require(self.remaining >= 0, "Nuxt reference budget exceeded")
        require(type(ref) is int, "Invalid Nuxt reference type")
        index = cast(int, ref)
        require(0 <= index < len(self.table), "Invalid or unsupported Nuxt reference")
        return index

    def _unwrap(self, ref: object, ancestors: tuple[int, ...]) -> tuple[int, tuple[int, ...]]:
        while True:
            index = self._index(ref)
            require(index not in ancestors, "Cyclic Nuxt reference")
            require(len(ancestors) < MAX_DEPTH, "Nuxt depth limit exceeded")
            raw = self.table[index]
            if isinstance(raw, list) and raw and isinstance(raw[0], str):
                if raw[0] == "LocalDate":
                    require(len(raw) == 2, "Invalid LocalDate wrapper")
                    return index, ancestors
                require(len(raw) == 2 and raw[0] in WRAPPERS, "Unsupported Nuxt wrapper")
                ancestors += (index,)
                ref = raw[1]
            else:
                return index, ancestors

    def read(self, *path: str) -> object:
        ref: object = 0
        ancestors: tuple[int, ...] = ()
        for name in path:
            index, ancestors = self._unwrap(ref, ancestors)
            record = mapping(self.table[index])
            require(name in record, f"Missing Nuxt field: {name}")
            ancestors += (index,)
            ref = record[name]
        return self._decode(ref, ancestors)

    def _decode(self, ref: object, ancestors: tuple[int, ...]) -> object:
        index, ancestors = self._unwrap(ref, ancestors)
        if index in self.cache:
            return self.cache[index]
        raw = self.table[index]
        chain = (*ancestors, index)
        if isinstance(raw, dict):
            result: object = {k: self._decode(v, chain) for k, v in raw.items()}
        elif isinstance(raw, list):
            if raw and raw[0] == "LocalDate":
                value = self._decode(raw[1], chain)
                require(
                    isinstance(value, str)
                    and re.fullmatch(r"\d{4}-\d{2}-\d{2}T00:00:00", value) is not None,
                    "Unsupported LocalDate value",
                )
                try:
                    result = datetime.fromisoformat(cast(str, value)).date().isoformat()
                except ValueError as exc:
                    raise RainbowDetailError("Invalid LocalDate value") from exc
            else:
                result = [self._decode(v, chain) for v in raw]
        else:
            require(raw is None or type(raw) in {str, bool, int, Decimal}, "Invalid Nuxt scalar")
            result = raw
        self.cache[index] = result
        return result
