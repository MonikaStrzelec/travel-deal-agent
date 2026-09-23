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


def test_ignores_unrelated_requests() -> None:
    # Arrange.
    c = capture()

    # Act.
    c.started(request_mock("/api/other/v1"))

    # Assert.
    assert c.count == 0 and not c.pending and not c.disabled


def test_ignores_get_requests_to_listing_paths() -> None:
    # Arrange.
    c = capture()

    # Act.
    c.started(request_mock(SEARCH_PATH, method="GET"))

    # Assert.
    assert c.count == 0


def test_captures_matching_search_exchange() -> None:
    # Arrange.
    c = capture()
    body = json.dumps({"Sortowanie": "cena-asc"}).encode("utf-8")
    request = request_mock(SEARCH_PATH, body=body)
    respond(request, body=b'{"Wynik": [], "CzyCenaZaOsobe": true}')

    # Act.
    c.started(request)
    c.finished(request)

    # Assert.
    assert len(c.exchanges) == 1
    assert c.exchanges[0].path == SEARCH_PATH
    assert c.exchanges[0].request == {"Sortowanie": "cena-asc"}
    assert c.exchanges[0].response == {"Wynik": [], "CzyCenaZaOsobe": True}
    assert request not in c.pending


def test_request_budget_exceeded_disables_capture() -> None:
    # Arrange.
    c = capture()

    # Act.
    for _ in range(MAX_CAPTURE_REQUESTS + 1):
        c.started(request_mock())

    # Assert.
    assert c.disabled and c.exchanges == [] and c.pending == {}


def test_oversized_request_body_disables_capture() -> None:
    # Arrange.
    c = capture()

    # Act.
    c.started(request_mock(body=b"x" * 200_000))

    # Assert.
    assert c.disabled


def test_unreadable_request_body_disables_capture() -> None:
    # Arrange.
    c = capture()

    # Act.
    c.started(request_mock(body=b"not json"))

    # Assert.
    assert c.disabled


def test_failed_pending_request_disables_capture_without_retry() -> None:
    # Arrange.
    c = capture()
    request = request_mock()
    c.started(request)

    # Act.
    c.failed(request)

    # Assert.
    assert c.disabled


def test_unsuccessful_response_disables_capture() -> None:
    # Arrange.
    c = capture()
    request = request_mock()
    respond(request, status=500)
    c.started(request)

    # Act.
    c.finished(request)

    # Assert.
    assert c.disabled


def test_oversized_response_disables_capture() -> None:
    # Arrange.
    c = capture()
    request = request_mock()
    respond(request, body=b'{"Wynik": [], "CzyCenaZaOsobe": true}')
    request.response.return_value.headers = {"content-length": "3000000"}
    c.started(request)

    # Act.
    c.finished(request)

    # Assert.
    assert c.disabled


def test_blocked_response_status_raises_on_check() -> None:
    # Arrange.
    c = capture()
    request = request_mock()
    response = respond(request, status=403)

    # Act.
    c.response(response)

    # Assert.
    with pytest.raises(RainbowBlocked):
        c.check()


def test_synchronize_times_out_and_disables() -> None:
    # Arrange.
    page = page_mock()
    page.wait_for_event.side_effect = PlaywrightTimeout("no matching pair arrived")
    c = ListingCapture(cast(Page, page))

    # Act.
    c.synchronize({"sortowanie": ["cena-asc"]}, timeout=lambda: 100.0)

    # Assert.
    assert c.disabled


def test_synchronize_skips_wait_once_already_disabled() -> None:
    # Arrange.
    c = capture()
    c.disable("test setup")
    page = cast(MagicMock, c.page)

    # Act.
    c.synchronize({"sortowanie": ["cena-asc"]}, timeout=lambda: 100.0)

    # Assert.
    page.wait_for_event.assert_not_called()


def test_close_removes_all_listeners() -> None:
    # Arrange.
    c = capture()
    page = cast(MagicMock, c.page)

    # Act.
    c.close()

    # Assert.
    assert page.remove_listener.call_count == 4
