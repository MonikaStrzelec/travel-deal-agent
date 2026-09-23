"""Passive bounded capture of the search page's own listing responses."""

import logging
from collections.abc import Callable
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Request, Response
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .rainbow_errors import RainbowBlocked
from .rainbow_listing_data import (
    CARDS_PATH,
    MAX_JSON_BYTES,
    SEARCH_PATH,
    ListingExchange,
    listing_json,
    matched_batches,
)
from .rainbow_nuxt import RainbowDetailError, mapping

logger = logging.getLogger(__name__)
MAX_CAPTURE_REQUESTS = 64
MAX_CAPTURE_BYTES = 16_000_000
MAX_RETAINED_EXCHANGES = 8


class ListingCapture:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.exchanges: list[ListingExchange] = []
        self.pending: dict[Request, tuple[int, dict[str, object]]] = {}
        self.count = 0
        self.size = 0
        self.disabled = False
        self.waited = False
        self.blocked: int | None = None
        page.on("request", self.started)
        page.on("response", self.response)
        page.on("requestfinished", self.finished)
        page.on("requestfailed", self.failed)

    @staticmethod
    def path(request: Request) -> str | None:
        url = urlsplit(request.url)
        if (
            url.scheme == "https"
            and url.netloc == "r.pl"
            and request.method == "POST"
            and url.path in {SEARCH_PATH, CARDS_PATH}
        ):
            return url.path
        return None

    def disable(self, reason: str) -> None:
        if not self.disabled:
            logger.warning("Rainbow structured listing unavailable: %s", reason)
        self.disabled = True
        self.pending.clear()
        self.exchanges.clear()

    def started(self, request: Request) -> None:
        if self.disabled or self.path(request) is None:
            return
        self.count += 1
        if self.count > MAX_CAPTURE_REQUESTS:
            self.disable("capture request budget exceeded")
            return
        try:
            body = (request.post_data or "").encode("utf-8")
            if len(body) > 128_000:
                self.disable("request body exceeds capture limit")
                return
            self.pending[request] = (self.count, mapping(listing_json(body)))
        except (RainbowDetailError, PlaywrightError, UnicodeError):
            self.disable("unreadable listing request")

    def response(self, response: Response) -> None:
        if self.path(response.request) is not None and response.status in {403, 429}:
            self.blocked = response.status

    def check(self) -> None:
        if self.blocked is not None:
            raise RainbowBlocked(f"Rainbow listing HTTP {self.blocked}")

    def failed(self, request: Request) -> None:
        if request in self.pending:
            self.disable("listing request failed; no retry")

    def finished(self, request: Request) -> None:
        pending = self.pending.pop(request, None)
        if self.disabled or pending is None:
            return
        try:
            response = request.response()
            if response is None or response.status != 200:
                self.disable("unsuccessful listing response")
                return
            length = response.headers.get("content-length")
            if length is not None and int(length) > MAX_JSON_BYTES:
                self.disable("listing response too large")
                return
            body = response.body()  # Read when finished, before any detail navigation.
            self.size += len(body)
            if self.size > MAX_CAPTURE_BYTES:
                self.disable("capture byte budget exceeded")
                return
            parsed = listing_json(body)
            order, payload = pending
            path = self.path(request)
            assert path is not None
            self.exchanges.append(ListingExchange(order, path, payload, parsed))
            self.exchanges.sort(key=lambda e: e.order)
            self.exchanges = self.exchanges[-MAX_RETAINED_EXCHANGES:]
        except (RainbowDetailError, PlaywrightError, ValueError):
            self.disable("unreadable listing response")

    def has_pair(self, expected: dict[str, list[str]]) -> bool:
        if self.pending:
            return False
        try:
            return bool(matched_batches(self.exchanges, expected))
        except RainbowDetailError:
            self.disable("unsupported listing response structure")
            return False

    def synchronize(self, expected: dict[str, list[str]], timeout: Callable[[], float]) -> None:
        self.check()
        if self.disabled or self.has_pair(expected) or self.waited:
            return
        self.waited = True
        try:
            self.page.wait_for_event(
                "requestfinished",
                predicate=lambda _: (
                    self.disabled or self.blocked is not None or self.has_pair(expected)
                ),
                timeout=timeout(),
            )
        except PlaywrightTimeout:
            self.disable("matching listing responses did not arrive before timeout")
        self.check()

    def close(self) -> None:
        self.page.remove_listener("request", self.started)
        self.page.remove_listener("response", self.response)
        self.page.remove_listener("requestfinished", self.finished)
        self.page.remove_listener("requestfailed", self.failed)
