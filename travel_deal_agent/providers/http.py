"""Bounded read-only HTTP access with explicit robots checks and no redirects."""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.client import HTTPMessage
from typing import Protocol
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


@dataclass(frozen=True)
class Response:
    status: int
    text: str
    headers: Mapping[str, str]


class Transport(Protocol):
    def get(self, url: str, timeout: float) -> Response: ...


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        return None


class UrllibTransport:
    """No cookies, credentials, JS execution, redirects, or implicit HTTP retries."""

    def get(self, url: str, timeout: float) -> Response:
        request = Request(
            url,
            headers={
                "User-Agent": "TravelDealAgent/0.1",
                "Accept": "text/html,text/plain",
                "Accept-Encoding": "identity",
            },
        )
        opener = build_opener(NoRedirect())
        try:
            with opener.open(request, timeout=timeout) as response:
                data = response.read(4_000_001)
                if len(data) > 4_000_000:
                    raise ValueError("HTTP response exceeds 4 MB")
                headers = dict(response.headers.items())
                return Response(response.status, data.decode("utf-8-sig"), headers)
        except HTTPError as exc:
            # Never read a challenge body or follow a redirect to another route.
            status = exc.code
            headers = dict(exc.headers.items())
            exc.close()
            return Response(status, "", headers)


class RequestBudget:
    def __init__(
        self,
        transport: Transport,
        max_requests: int,
        timeout: float,
        cycle_seconds: float,
        gap: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport
        self.max_requests = max_requests
        self.timeout = timeout
        self.deadline = clock() + cycle_seconds
        self.gap = gap
        self.clock = clock
        self.sleep = sleep
        self.calls = 0
        self.last_end: float | None = None

    def get(self, url: str) -> Response:
        if self.calls >= self.max_requests:
            raise ValueError("ITAKA request budget exhausted")
        delay = (
            max(0.0, self.gap - (self.clock() - self.last_end)) if self.last_end is not None else 0
        )
        if self.clock() + delay >= self.deadline:
            raise ValueError("ITAKA cycle deadline exceeded")
        self.sleep(delay)
        self.calls += 1
        response = self.transport.get(url, min(self.timeout, self.deadline - self.clock()))
        self.last_end = self.clock()
        if self.last_end >= self.deadline:
            raise ValueError("ITAKA cycle deadline exceeded")
        if response.status != 200:
            raise ValueError(f"ITAKA HTTP {response.status}; no automatic retry")
        return response
