"""Offline checks for passive listing capture; no browser or network access."""

import json
from typing import cast
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Page, Request, Response
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from travel_deal_agent.providers.rainbow_capture import MAX_CAPTURE_REQUESTS, ListingCapture
from travel_deal_agent.providers.rainbow_errors import RainbowBlocked
from travel_deal_agent.providers.rainbow_listing_data import SEARCH_PATH


def page_mock() -> MagicMock:
    return MagicMock(spec=Page)


def request_mock(path: str = SEARCH_PATH, method: str = "POST", body: bytes = b"{}") -> MagicMock:
    request = MagicMock(spec=Request)
    request.url = f"https://r.pl{path}"
    request.method = method
    request.post_data = body.decode("utf-8")
    return request


def respond(request: MagicMock, status: int = 200, body: bytes = b"{}") -> MagicMock:
    response = MagicMock(spec=Response)
    response.request = request
    response.status = status
    response.body.return_value = body
    response.headers = {"content-length": str(len(body))}
    request.response.return_value = response
    return response


def capture() -> ListingCapture:
    return ListingCapture(cast(Page, page_mock()))


@pytest.mark.parametrize(
    "path,method",
    [
        pytest.param("/api/other/v1", "POST", id="unrelated_path"),
        pytest.param(SEARCH_PATH, "GET", id="get_to_listing_path"),
    ],
)
def test_ignores_requests_other_than_listing_posts(path: str, method: str) -> None:
    c = capture()

    c.started(request_mock(path, method=method))

    assert c.count == 0 and not c.pending and not c.disabled


def test_captures_matching_search_exchange() -> None:
    c = capture()
    body = json.dumps({"Sortowanie": "cena-asc"}).encode("utf-8")
    request = request_mock(SEARCH_PATH, body=body)
    respond(request, body=b'{"Wynik": [], "CzyCenaZaOsobe": true}')

    c.started(request)
    c.finished(request)

    assert len(c.exchanges) == 1
    assert c.exchanges[0].path == SEARCH_PATH
    assert c.exchanges[0].request == {"Sortowanie": "cena-asc"}
    assert c.exchanges[0].response == {"Wynik": [], "CzyCenaZaOsobe": True}
    assert request not in c.pending


def test_request_budget_exceeded_disables_capture() -> None:
    c = capture()

    for _ in range(MAX_CAPTURE_REQUESTS + 1):
        c.started(request_mock())

    assert c.disabled and c.exchanges == [] and c.pending == {}


@pytest.mark.parametrize(
    "body",
    [pytest.param(b"x" * 200_000, id="oversized"), pytest.param(b"not json", id="unreadable")],
)
def test_unusable_request_body_disables_capture(body: bytes) -> None:
    c = capture()

    c.started(request_mock(body=body))

    assert c.disabled


def test_failed_pending_request_disables_capture_without_retry() -> None:
    c = capture()
    request = request_mock()
    c.started(request)

    c.failed(request)

    assert c.disabled


@pytest.mark.parametrize(
    "status,content_length",
    [
        pytest.param(500, None, id="unsuccessful_status"),
        pytest.param(200, "3000000", id="oversized_response"),
    ],
)
def test_unusable_response_disables_capture(status: int, content_length: str | None) -> None:
    c = capture()
    request = request_mock()
    respond(request, status=status, body=b'{"Wynik": [], "CzyCenaZaOsobe": true}')
    if content_length is not None:
        request.response.return_value.headers = {"content-length": content_length}
    c.started(request)

    c.finished(request)

    assert c.disabled


def test_blocked_response_status_raises_on_check() -> None:
    c = capture()
    request = request_mock()
    response = respond(request, status=403)

    c.response(response)

    with pytest.raises(RainbowBlocked):
        c.check()


def test_synchronize_times_out_and_disables() -> None:
    page = page_mock()
    page.wait_for_event.side_effect = PlaywrightTimeout("no matching pair arrived")
    c = ListingCapture(cast(Page, page))

    c.synchronize({"sortowanie": ["cena-asc"]}, timeout=lambda: 100.0)

    assert c.disabled


def test_synchronize_skips_wait_once_already_disabled() -> None:
    c = capture()
    c.disable("test setup")
    page = cast(MagicMock, c.page)

    c.synchronize({"sortowanie": ["cena-asc"]}, timeout=lambda: 100.0)

    page.wait_for_event.assert_not_called()


def test_close_removes_all_listeners() -> None:
    c = capture()
    page = cast(MagicMock, c.page)

    c.close()

    assert page.remove_listener.call_count == 4
