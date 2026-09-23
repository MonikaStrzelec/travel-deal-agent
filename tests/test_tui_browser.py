"""Offline checks for the minimal TUI Playwright captures; no real browser/network."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Page, Response

from travel_deal_agent.providers.tui_browser import (
    PRICE_CHECK_PATH,
    SEARCH_OFFERS_PATH,
    _matches_path,
    _select_response,
    capture_offer_price,
    capture_search_offers,
)
from travel_deal_agent.providers.tui_errors import (
    TuiBlocked,
    TuiError,
    TuiStructureError,
    TuiTimeout,
)

URL = "https://www.tui.pl" + SEARCH_OFFERS_PATH
DETAIL_URL = "https://www.tui.pl/wypoczynek/turcja/sun-city-ayt43083/OfferCodeWS/ABC"
PRICE_URL = "https://www.tui.pl" + PRICE_CHECK_PATH + "?offerCode=ABC&mode=REALTIME"


def fake_response(status: int = 200, url: str = URL, body: str = "{}") -> SimpleNamespace:
    return SimpleNamespace(status=status, url=url, text=lambda: body)


# --- pure matching/selection logic ------------------------------------------------


@pytest.mark.parametrize(
    "url,path,expected",
    [
        (URL, SEARCH_OFFERS_PATH, True),
        (URL + "?foo=bar", SEARCH_OFFERS_PATH, True),
        (
            "https://www.tui.pl/api/services/tui-search/api/search/offers/count",
            SEARCH_OFFERS_PATH,
            False,
        ),
        (
            "https://www.tui.pl/api/services/tui-search/api/listing/filters",
            SEARCH_OFFERS_PATH,
            False,
        ),
        ("https://www.tui.pl/wypoczynek/wyniki-wyszukiwania-samolot", SEARCH_OFFERS_PATH, False),
        ("https://evil.example" + SEARCH_OFFERS_PATH, SEARCH_OFFERS_PATH, False),
        (PRICE_URL, PRICE_CHECK_PATH, True),
        (URL, PRICE_CHECK_PATH, False),
        ("https://www.tui.pl" + PRICE_CHECK_PATH + "/count", PRICE_CHECK_PATH, False),
    ],
)
def test_matches_path_matches_by_host_and_exact_path_only(
    url: str, path: str, expected: bool
) -> None:
    # Arrange / Act / Assert.
    assert _matches_path(url, path) is expected


def test_select_response_returns_the_one_match() -> None:
    # Arrange.
    response = cast(Response, fake_response(200))
    # Act / Assert.
    assert _select_response([response], "search/offers") is response


def test_select_response_raises_timeout_when_none_observed() -> None:
    # Arrange / Act / Assert.
    with pytest.raises(TuiTimeout):
        _select_response([], "search/offers")


def test_select_response_raises_on_multiple_matches() -> None:
    # Arrange: never guess which of several responses reflects the current search.
    responses = [fake_response(200), fake_response(200)]
    # Act / Assert.
    with pytest.raises(TuiStructureError, match="Ambiguous"):
        _select_response(cast(list[Response], responses), "search/offers")


@pytest.mark.parametrize("status", [403, 429])
def test_select_response_raises_blocked_on_403_429(status: int) -> None:
    # Arrange / Act / Assert.
    with pytest.raises(TuiBlocked):
        _select_response([cast(Response, fake_response(status))], "search/offers")


def test_select_response_raises_generic_error_on_other_bad_status() -> None:
    # Arrange / Act / Assert.
    with pytest.raises(TuiError):
        _select_response([cast(Response, fake_response(500))], "search/offers")


# --- shared Playwright mocking helpers ---------------------------------------------


def page_mock() -> MagicMock:
    page = MagicMock(spec=Page)
    page.locator.return_value.inner_text.return_value = "TUI wyniki wyszukiwania"
    page.goto.return_value = MagicMock(status=200)
    return page


def playwright_context_manager(page: MagicMock) -> tuple[MagicMock, MagicMock]:
    context = MagicMock()
    context.new_page.return_value = page
    browser = MagicMock()
    browser.new_context.return_value = context
    playwright = MagicMock()
    playwright.chromium.launch.return_value = browser
    cm = MagicMock()
    cm.__enter__.return_value = playwright
    cm.__exit__.return_value = False
    return cm, browser


# --- capture_search_offers, with a mocked Playwright --------------------------


def test_capture_returns_the_matching_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: the response handler fires synchronously during goto(), as it would
    # once the page's own JS issues the request while navigating.
    page = page_mock()
    handlers: dict[str, object] = {}
    page.on.side_effect = lambda event, handler: handlers.__setitem__(event, handler)
    response = fake_response(200, body='{"offers": []}')

    def goto(*args: object, **kwargs: object) -> MagicMock:
        handlers["response"](response)  # type: ignore[operator]
        return MagicMock(status=200)

    page.goto.side_effect = goto
    cm, browser = playwright_context_manager(page)
    monkeypatch.setattr("travel_deal_agent.providers.tui_browser.sync_playwright", lambda: cm)

    # Act.
    result = capture_search_offers(URL, timeout_seconds=5)

    # Assert.
    assert result == '{"offers": []}'
    browser.close.assert_called_once()
    page.goto.assert_called_once()
    assert page.goto.call_args.args[0] == URL


def test_capture_ignores_unrelated_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: an unrelated xhr (e.g. listing/filters) must not be mistaken for offers.
    page = page_mock()
    handlers: dict[str, object] = {}
    page.on.side_effect = lambda event, handler: handlers.__setitem__(event, handler)
    unrelated = fake_response(
        200, url="https://www.tui.pl/api/services/tui-search/api/listing/filters"
    )
    target = fake_response(200, body='{"offers": [1]}')

    def goto(*args: object, **kwargs: object) -> MagicMock:
        handlers["response"](unrelated)  # type: ignore[operator]
        handlers["response"](target)  # type: ignore[operator]
        return MagicMock(status=200)

    page.goto.side_effect = goto
    cm, browser = playwright_context_manager(page)
    monkeypatch.setattr("travel_deal_agent.providers.tui_browser.sync_playwright", lambda: cm)

    # Act.
    result = capture_search_offers(URL, timeout_seconds=5)

    # Assert.
    assert result == '{"offers": [1]}'


def test_capture_raises_on_navigation_block(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange.
    page = page_mock()
    page.goto.return_value = MagicMock(status=403)
    cm, browser = playwright_context_manager(page)
    monkeypatch.setattr("travel_deal_agent.providers.tui_browser.sync_playwright", lambda: cm)

    # Act / Assert.
    with pytest.raises(TuiBlocked):
        capture_search_offers(URL, timeout_seconds=5)
    browser.close.assert_called_once()


def test_capture_raises_on_blocked_page_text(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange.
    page = page_mock()
    page.locator.return_value.inner_text.return_value = "Please verify you are human"
    cm, browser = playwright_context_manager(page)
    monkeypatch.setattr("travel_deal_agent.providers.tui_browser.sync_playwright", lambda: cm)

    # Act / Assert.
    with pytest.raises(TuiBlocked):
        capture_search_offers(URL, timeout_seconds=5)
    browser.close.assert_called_once()


def test_capture_times_out_when_no_matching_response_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange: goto() never fires a matching response; the injected clock crosses
    # the deadline on the first check, so no real waiting happens in the test.
    page = page_mock()
    cm, browser = playwright_context_manager(page)
    monkeypatch.setattr("travel_deal_agent.providers.tui_browser.sync_playwright", lambda: cm)
    ticks = iter([0.0, 100.0, 100.0])

    # Act / Assert.
    with pytest.raises(TuiTimeout):
        capture_search_offers(URL, timeout_seconds=5, clock=lambda: next(ticks))
    browser.close.assert_called_once()


def test_capture_raises_ambiguous_on_multiple_matching_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange.
    page = page_mock()
    handlers: dict[str, object] = {}
    page.on.side_effect = lambda event, handler: handlers.__setitem__(event, handler)

    def goto(*args: object, **kwargs: object) -> MagicMock:
        handlers["response"](fake_response(200))  # type: ignore[operator]
        handlers["response"](fake_response(200))  # type: ignore[operator]
        return MagicMock(status=200)

    page.goto.side_effect = goto
    cm, browser = playwright_context_manager(page)
    monkeypatch.setattr("travel_deal_agent.providers.tui_browser.sync_playwright", lambda: cm)

    # Act / Assert.
    with pytest.raises(TuiStructureError, match="Ambiguous"):
        capture_search_offers(URL, timeout_seconds=5)
    browser.close.assert_called_once()


def test_capture_never_calls_page_request_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    # A defensive regression: this module must only ever navigate to the caller-
    # supplied /wypoczynek/... URL and passively listen; it must never issue its own
    # request (e.g. via page.request / context.request) to /api/...
    page = page_mock()
    handlers: dict[str, object] = {}
    page.on.side_effect = lambda event, handler: handlers.__setitem__(event, handler)
    response = fake_response(200, body="{}")

    def goto(*args: object, **kwargs: object) -> MagicMock:
        handlers["response"](response)  # type: ignore[operator]
        return MagicMock(status=200)

    page.goto.side_effect = goto
    cm, browser = playwright_context_manager(page)
    monkeypatch.setattr("travel_deal_agent.providers.tui_browser.sync_playwright", lambda: cm)

    # Act.
    capture_search_offers(URL, timeout_seconds=5)

    # Assert: goto is the only navigation-like call; no direct request API used.
    assert page.method_calls
    called_names = {call[0] for call in page.method_calls}
    assert "request" not in called_names
    assert not hasattr(page, "request") or not page.request.called


# --- capture_offer_price, with a mocked Playwright -----------------------------
# One candidate offer per cycle at most; same passive, no-manual-/api/-request
# contract as capture_search_offers, just against the offer's own detail page.


def test_capture_offer_price_returns_the_matching_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange.
    page = page_mock()
    handlers: dict[str, object] = {}
    page.on.side_effect = lambda event, handler: handlers.__setitem__(event, handler)
    response = fake_response(200, url=PRICE_URL, body='{"offerStatus": "AVAILABLE"}')

    def goto(*args: object, **kwargs: object) -> MagicMock:
        handlers["response"](response)  # type: ignore[operator]
        return MagicMock(status=200)

    page.goto.side_effect = goto
    cm, browser = playwright_context_manager(page)
    monkeypatch.setattr("travel_deal_agent.providers.tui_browser.sync_playwright", lambda: cm)

    # Act.
    result = capture_offer_price(DETAIL_URL, timeout_seconds=5)

    # Assert.
    assert result == '{"offerStatus": "AVAILABLE"}'
    browser.close.assert_called_once()
    page.goto.assert_called_once()
    assert page.goto.call_args.args[0] == DETAIL_URL


def test_capture_offer_price_ignores_unrelated_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange: a detail page also triggers offerData, analytics, alternatives, etc.
    page = page_mock()
    handlers: dict[str, object] = {}
    page.on.side_effect = lambda event, handler: handlers.__setitem__(event, handler)
    unrelated = fake_response(
        200, url="https://www.tui.pl/api/www/hotel-cards/offerData?offerCode=ABC"
    )
    target = fake_response(200, url=PRICE_URL, body='{"offerStatus": "AVAILABLE"}')

    def goto(*args: object, **kwargs: object) -> MagicMock:
        handlers["response"](unrelated)  # type: ignore[operator]
        handlers["response"](target)  # type: ignore[operator]
        return MagicMock(status=200)

    page.goto.side_effect = goto
    cm, browser = playwright_context_manager(page)
    monkeypatch.setattr("travel_deal_agent.providers.tui_browser.sync_playwright", lambda: cm)

    # Act.
    result = capture_offer_price(DETAIL_URL, timeout_seconds=5)

    # Assert.
    assert result == '{"offerStatus": "AVAILABLE"}'


def test_capture_offer_price_never_matches_the_search_offers_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A defensive regression: the two captures must never cross-match each other's
    # response, even though one path is a prefix-related sibling of the other.
    page = page_mock()
    handlers: dict[str, object] = {}
    page.on.side_effect = lambda event, handler: handlers.__setitem__(event, handler)
    search_response = fake_response(200, url=URL, body='{"offers": []}')

    def goto(*args: object, **kwargs: object) -> MagicMock:
        handlers["response"](search_response)  # type: ignore[operator]
        return MagicMock(status=200)

    page.goto.side_effect = goto
    cm, browser = playwright_context_manager(page)
    monkeypatch.setattr("travel_deal_agent.providers.tui_browser.sync_playwright", lambda: cm)
    ticks = iter([0.0, 100.0, 100.0])

    # Act / Assert.
    with pytest.raises(TuiTimeout):
        capture_offer_price(DETAIL_URL, timeout_seconds=5, clock=lambda: next(ticks))


def test_capture_offer_price_times_out_when_no_matching_response_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange.
    page = page_mock()
    cm, browser = playwright_context_manager(page)
    monkeypatch.setattr("travel_deal_agent.providers.tui_browser.sync_playwright", lambda: cm)
    ticks = iter([0.0, 100.0, 100.0])

    # Act / Assert.
    with pytest.raises(TuiTimeout):
        capture_offer_price(DETAIL_URL, timeout_seconds=5, clock=lambda: next(ticks))
    browser.close.assert_called_once()


def test_capture_offer_price_raises_ambiguous_on_multiple_matching_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange.
    page = page_mock()
    handlers: dict[str, object] = {}
    page.on.side_effect = lambda event, handler: handlers.__setitem__(event, handler)

    def goto(*args: object, **kwargs: object) -> MagicMock:
        handlers["response"](fake_response(200, url=PRICE_URL))  # type: ignore[operator]
        handlers["response"](fake_response(200, url=PRICE_URL))  # type: ignore[operator]
        return MagicMock(status=200)

    page.goto.side_effect = goto
    cm, browser = playwright_context_manager(page)
    monkeypatch.setattr("travel_deal_agent.providers.tui_browser.sync_playwright", lambda: cm)

    # Act / Assert.
    with pytest.raises(TuiStructureError, match="Ambiguous"):
        capture_offer_price(DETAIL_URL, timeout_seconds=5)
    browser.close.assert_called_once()


def test_capture_offer_price_never_calls_page_request_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same defensive regression as the search capture: only page.goto(), never a
    # direct page/context request to /api/....
    page = page_mock()
    handlers: dict[str, object] = {}
    page.on.side_effect = lambda event, handler: handlers.__setitem__(event, handler)
    response = fake_response(200, url=PRICE_URL, body="{}")

    def goto(*args: object, **kwargs: object) -> MagicMock:
        handlers["response"](response)  # type: ignore[operator]
        return MagicMock(status=200)

    page.goto.side_effect = goto
    cm, browser = playwright_context_manager(page)
    monkeypatch.setattr("travel_deal_agent.providers.tui_browser.sync_playwright", lambda: cm)

    # Act.
    capture_offer_price(DETAIL_URL, timeout_seconds=5)

    # Assert.
    assert page.method_calls
    called_names = {call[0] for call in page.method_calls}
    assert "request" not in called_names
    assert not hasattr(page, "request") or not page.request.called
