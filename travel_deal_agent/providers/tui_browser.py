"""Minimal Playwright capture of TUI's own client-side JSON responses.

Much simpler than the Rainbow browser module: both captures below need one
passively observed response body, not card enumeration, scrolling, or a manual
fetch. Neither ever calls `/api/...` itself -- each opens a confirmed-allowed
`/wypoczynek/...` URL (a search-results URL built by `tui_query.build_search_path`,
or one specific offer's own detail-page URL) and listens only to responses the
page's own JavaScript triggers.

Correlation with the current navigation (never using a stale/unrelated response):
a fresh Chromium instance, context and page are created for every call and closed
at the end, so no interaction from an earlier, different navigation can leak in.
Within that one navigation, exactly one response matching the target path is
required; zero is a timeout and more than one is treated as ambiguous (rather than
guessing which reflects the current page). One prior manual reconnaissance run
observed exactly one such response per navigation for both targets, which is what
this bound assumes.
"""

import re
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Response, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .tui_errors import TuiBlocked, TuiError, TuiStructureError, TuiTimeout

SEARCH_OFFERS_PATH = "/api/services/tui-search/api/search/offers"
PRICE_CHECK_PATH = "/api/services/tui-search/api/search/offers/price"
BLOCKED_TEXT = re.compile(r"captcha|verify you are human|access denied", re.I)


def _matches_path(url: str, path: str) -> bool:
    """Match by host and exact path only, never by guessing query parameters."""
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc == "www.tui.pl" and parts.path == path


def _select_response(responses: list[Response], label: str) -> Response:
    """Pick the one response that unambiguously belongs to this navigation."""
    if not responses:
        raise TuiTimeout(f"No matching TUI {label} response observed before timeout")
    if len(responses) > 1:
        raise TuiStructureError(f"Ambiguous: more than one TUI {label} response observed")
    response = responses[0]
    if response.status in (403, 429):
        raise TuiBlocked(f"TUI {label} HTTP {response.status}")
    if response.status != 200:
        raise TuiError(f"TUI {label} HTTP {response.status}")
    return response


def _capture_one_response(
    url: str,
    target_path: str,
    label: str,
    timeout_seconds: float,
    clock: Callable[[], float],
) -> str:
    """Open `url`, passively wait for the one response matching `target_path`, return its body.

    No offer card is opened, no scrolling occurs, and no request is made to
    `/api/...` by this function itself -- only `page.goto(url)` to the caller-supplied,
    already-confirmed-allowed `/wypoczynek/...` URL. `clock` is injected only so the
    bounded poll loop below is deterministically testable, mirroring the rest of the
    project's clock-injection convention.
    """
    timeout_ms = timeout_seconds * 1000
    responses: list[Response] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, timeout=timeout_ms)
        try:
            context = browser.new_context(locale="pl-PL", viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            page.on(
                "response",
                lambda response: (
                    responses.append(response) if _matches_path(response.url, target_path) else None
                ),
            )
            try:
                navigation = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if navigation is not None and navigation.status in (403, 429):
                    raise TuiBlocked(f"TUI navigation HTTP {navigation.status}")
                body_text = page.locator("body").inner_text(timeout=timeout_ms)
                if BLOCKED_TEXT.search(body_text):
                    raise TuiBlocked("TUI requires human verification or denies access")
                deadline = clock() + timeout_seconds
                while not responses and clock() < deadline:
                    page.wait_for_timeout(200)
                response = _select_response(responses, label)
                return response.text()
            except PlaywrightTimeout as exc:
                raise TuiTimeout("TUI browser operation timed out") from exc
            except PlaywrightError as exc:
                raise TuiError("TUI browser operation failed") from exc
        finally:
            browser.close()


def capture_search_offers(
    url: str,
    timeout_seconds: float = 20.0,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Open a search-results `url`, return the passively observed `search/offers` body."""
    return _capture_one_response(url, SEARCH_OFFERS_PATH, "search/offers", timeout_seconds, clock)


def capture_offer_price(
    url: str,
    timeout_seconds: float = 20.0,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Open one offer's own detail-page `url`, return the passively observed
    `search/offers/price?...mode=REALTIME` body that the detail page itself triggers.

    `url` must be the offer's own `/wypoczynek/.../OfferCodeWS/<offerCode>` detail
    page -- confirmed allowed by `robots.txt` in prior reconnaissance -- never a
    configurator, checkout or reservation path. This function never requests
    `/api/...` itself and never clicks, scrolls or fills any form; it only opens the
    page and passively observes the one response TUI's own detail page triggers.
    """
    return _capture_one_response(
        url, PRICE_CHECK_PATH, "search/offers/price", timeout_seconds, clock
    )
