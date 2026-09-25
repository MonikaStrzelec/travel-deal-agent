"""Read embedded Flight data without executing JavaScript or fetching resources."""

import json
import re
from decimal import Decimal
from html.parser import HTMLParser

from .boundary import mapping


class Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.inside = False
        self.parts: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.inside = True
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.inside:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.inside:
            self.scripts.append("".join(self.parts))
            self.inside = False


class FlightData:
    """Decode JSON and byte-counted text records and resolve explicit data references."""

    def __init__(self, html: str) -> None:
        parser = Scripts()
        parser.feed(html)
        chunks: list[str] = []
        for script in parser.scripts:
            match = re.fullmatch(r"self\.__next_f\.push\((.*)\);?", script.strip(), re.S)
            if match:
                value: object = json.loads(match[1])
                if isinstance(value, list) and len(value) == 2 and value[0] == 1:
                    if not isinstance(value[1], str):
                        raise ValueError("Invalid Flight chunk")
                    chunks.append(value[1])
        if not chunks:
            raise ValueError("Missing ITAKA detail data")
        stream = "".join(chunks).encode("utf-8")
        self.records: dict[str, object] = {}
        position = 0
        while position < len(stream):
            if stream[position : position + 1] == b":":
                end = stream.find(b"\n", position)
                if end < 0:
                    raise ValueError("Truncated Flight hint")
                position = end + 1
                continue
            header = re.match(rb"([0-9a-f]+):", stream[position:])
            if header is None:
                raise ValueError("Invalid Flight record framing")
            key = header[1].decode()
            position += header.end()
            if stream[position : position + 1] == b"T":
                size = re.match(rb"T([0-9a-f]+),", stream[position:])
                if size is None:
                    raise ValueError("Invalid Flight text size")
                position += size.end()
                end = position + int(size[1], 16)
                if end > len(stream):
                    raise ValueError("Truncated Flight text")
                record: object = stream[position:end].decode("utf-8")
                position = end
            else:
                end = stream.find(b"\n", position)
                if end < 0:
                    raise ValueError("Truncated Flight record")
                payload = stream[position:end]
                position = end + 1
                # Imports, hints and runtime diagnostics are not application data.
                if not payload or payload[:1] not in b'[{"0123456789-ntf':
                    continue
                record = json.loads(payload, parse_float=Decimal)
            if key in self.records:
                raise ValueError("Duplicate Flight record")
            self.records[key] = record

    def resolve(self, value: object, trail: tuple[str, ...] = ()) -> object:
        if len(trail) > 40:
            raise ValueError("Flight reference depth exceeded")
        if isinstance(value, str):
            if value == "$undefined":
                return None
            if value.startswith("$$"):
                return value[1:]
            match = re.fullmatch(r"\$@?([0-9a-f]+)(?::(.+))?", value)
            if match:
                if value in trail or match[1] not in self.records:
                    raise ValueError("Missing or cyclic Flight reference")
                current = self.records[match[1]]
                for part in (match[2] or "").split(":") if match[2] else []:
                    if isinstance(current, str):
                        current = self.resolve(current, (*trail, value))
                    if isinstance(current, list):
                        index = 3 if part == "props" else int(part)
                        current = current[index]
                    else:
                        current = mapping(current)[part]
                return self.resolve(current, (*trail, value))
            return value
        if isinstance(value, list):
            return [self.resolve(item, trail) for item in value]
        if isinstance(value, dict):
            return {key: self.resolve(item, trail) for key, item in mapping(value).items()}
        return value

    def objects(self) -> list[dict[str, object]]:
        """Inspect literal objects; resolve only selected data, never the component tree."""
        result: list[dict[str, object]] = []
        pending = list(self.records.values())
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                obj = mapping(item)
                result.append(obj)
                pending.extend(obj.values())
            elif isinstance(item, list):
                pending.extend(item)
        return result
