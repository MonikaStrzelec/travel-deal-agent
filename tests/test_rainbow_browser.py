"""Browser-boundary behavior with mocks only; no browser subprocess or requests."""

from datetime import date
from decimal import Decimal
from typing import cast
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from travel_deal_agent.config import Settings
from travel_deal_agent.models import Offer
from travel_deal_agent.providers.rainbow_browser import BrowserListing, open_listing
from travel_deal_agent.providers.rainbow_config import Limits, SearchPlan
from travel_deal_agent.providers.rainbow_errors import (
    RainbowBlocked,
    RainbowStructureError,
    RainbowTimeout,
)
from travel_deal_agent.providers.rainbow_listing_data import ListingEvidence
from travel_deal_agent.providers.rainbow_nuxt import MAX_HTML_BYTES, RainbowDetailError


def page_mock() -> MagicMock:
    page = MagicMock(spec=Page)
    page.locator.return_value.inner_text.return_value = "Rainbow"
    return page


@pytest.mark.parametrize("already_sorted", [True, False])
def test_final_sort_is_enforced_or_left_unchanged(
    already_sorted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: filters may reset the source's order to recommendations.
    page = page_mock()
    value = "cena-asc" if already_sorted else "rekomendowane-biznes-desc"
    page.url = f"https://r.pl/szukaj?sortowanie={value}"
    page.locator.return_value.input_value.return_value = value
    page.locator.return_value.get_by_role.return_value.count.return_value = 1
    page.get_by_role.return_value.count.return_value = 1
    listing = BrowserListing(cast(Page, page), Limits())
    listing.expected = {"cena.do": ["2000"]}
    ready = MagicMock()
    monkeypatch.setattr(listing, "ready", ready)

    # Act.
    listing.ensure_price_sort()

    # Assert: unchanged sorting avoids clicks, but both paths verify final readiness.
    assert listing.expected == {"cena.do": ["2000"], "sortowanie": ["cena-asc"]}
    ready.assert_called_once_with()
    if already_sorted:
        page.get_by_role.assert_not_called()
    else:
        page.get_by_role.assert_called_once_with("option", name="od najniższej ceny", exact=True)
        page.get_by_role.return_value.click.assert_called_once_with()


def test_final_sort_failure_is_not_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: source still fails to settle into ascending order.
    page = page_mock()
    page.url = "https://r.pl/szukaj?sortowanie=cena-asc"
    page.locator.return_value.input_value.return_value = "cena-asc"
    listing = BrowserListing(cast(Page, page), Limits())
    monkeypatch.setattr(listing, "ready", MagicMock(side_effect=RainbowTimeout("sort")))

    # Act / assert.
    with pytest.raises(RainbowTimeout):
        listing.ensure_price_sort()


def test_empty_ready_state_ignores_recommendation_cards() -> None:
    # Arrange: empty listings can still contain ten recommendation cards.
    page = page_mock()
    page.wait_for_function.return_value.json_value.return_value = "empty"
    page.locator.return_value.count.return_value = 10
    listing = BrowserListing(cast(Page, page), Limits())

    # Act.
    listing.ready()

    # Assert: neither recommendation cards nor missing sort UI cause a retry/read.
    assert listing.count() == 0
    assert listing.advance() is False
    with pytest.raises(RainbowStructureError, match="recommendation"):
        listing.card_html(0)
    page.wait_for_function.assert_called_once()
    page.wait_for_function.return_value.dispose.assert_called_once()


def test_wait_reports_block_instead_of_empty() -> None:
    # Arrange.
    page = page_mock()
    page.wait_for_function.return_value.json_value.return_value = "blocked"
    listing = BrowserListing(cast(Page, page), Limits())

    # Act / assert.
    with pytest.raises(RainbowBlocked):
        listing.ready()


def test_expired_cycle_does_not_start_another_browser_operation() -> None:
    # Arrange.
    ticks = iter([0.0, 181.0])
    page = page_mock()
    listing = BrowserListing(cast(Page, page), Limits(), clock=lambda: next(ticks))

    # Act / assert.
    with pytest.raises(RainbowTimeout):
        listing.count()
    page.locator.assert_not_called()


@pytest.mark.parametrize("identifier", ["Standard hotelu6", "Wyżywienie2-posilki"])
def test_selection_clicks_associated_label_not_obscured_input(
    monkeypatch: pytest.MonkeyPatch,
    identifier: str,
) -> None:
    # Arrange: reproduce the failed POC's overlaid native input.
    page = page_mock()
    target = MagicMock(spec=Locator)
    target.count.return_value = 1
    target.is_checked.return_value = False
    target.get_attribute.return_value = identifier
    label = MagicMock(spec=Locator)
    label.count.return_value = 1
    body = MagicMock(spec=Locator)
    body.inner_text.return_value = "Rainbow"
    missing = MagicMock(spec=Locator)
    missing.count.return_value = 0
    selectors = {"body": body, f'label[for="{identifier}"]': label}
    page.locator.side_effect = lambda selector: selectors.get(selector, missing)
    assertion = MagicMock()
    expectation = MagicMock(return_value=assertion)
    monkeypatch.setattr("travel_deal_agent.providers.rainbow_browser.expect", expectation)
    listing = BrowserListing(cast(Page, page), Limits())

    # Act.
    listing.check(cast(Locator, target))

    # Assert: regression for the intercepted checkbox click, with checked-state verification.
    page.locator.assert_any_call(f'label[for="{identifier}"]')
    label.click.assert_called_once_with()
    target.click.assert_not_called()
    target.check.assert_not_called()
    assertion.to_be_checked.assert_called_once()


def test_missing_control_is_a_structure_failure() -> None:
    # Arrange.
    page = page_mock()
    target = MagicMock(spec=Locator)
    target.count.return_value = 0
    listing = BrowserListing(cast(Page, page), Limits())

    # Act / assert.
    with pytest.raises(RainbowStructureError):
        listing.check(cast(Locator, target))
    target.click.assert_not_called()


def test_dynamic_control_is_allowed_to_mount_before_uniqueness_check() -> None:
    # Arrange: the control does not exist until its attachment wait completes.
    page = page_mock()
    target = MagicMock(spec=Locator)
    target.count.return_value = 0

    def mounted(**kwargs: object) -> None:
        target.count.return_value = 1

    target.wait_for.side_effect = mounted
    listing = BrowserListing(cast(Page, page), Limits())

    # Act / assert.
    assert listing.unique(cast(Locator, target)) is target
    target.wait_for.assert_called_once()


def test_airport_panel_waits_before_reading_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: panel and checkbox group mount in distinct steps.
    page = page_mock()
    panel, group, control = (MagicMock(spec=Locator) for _ in range(3))
    events: list[str] = []
    page.locator.return_value = panel
    panel.wait_for.side_effect = lambda **kwargs: events.append("panel-visible")
    panel.locator.return_value = group
    group.wait_for.side_effect = lambda **kwargs: events.append("group-visible")
    group.get_by_role.return_value = control
    control.wait_for.side_effect = lambda **kwargs: events.append("control-attached")
    assertion = MagicMock()
    assertion.to_have_count.side_effect = lambda *args, **kwargs: events.append("loading-cleared")
    assertion.to_be_enabled.side_effect = lambda **kwargs: events.append("control-enabled")
    monkeypatch.setattr("travel_deal_agent.providers.rainbow_browser.expect", lambda _: assertion)
    monkeypatch.setattr(BrowserListing, "guard", lambda _: None)

    # Act.
    result = BrowserListing(cast(Page, page), Limits()).airport_panel(("LCJ",))

    # Assert: readiness is checked before any selection; no arbitrary indexed locator.
    assert result is panel
    assert events == [
        "panel-visible",
        "group-visible",
        "loading-cleared",
        "control-attached",
        "control-enabled",
    ]
    group.get_by_role.assert_called_once_with("checkbox", name="Łódź", exact=True)
    control.click.assert_not_called()


def test_airport_panel_timeout_is_explicit() -> None:
    # Arrange.
    page = page_mock()
    page.locator.return_value.wait_for.side_effect = PlaywrightTimeout("panel missing")

    # Act / assert.
    with pytest.raises(RainbowTimeout, match="airport panel did not become ready"):
        BrowserListing(cast(Page, page), Limits()).airport_panel(("LCJ",))


def test_browser_startup_timeout_keeps_its_error_category(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange: mock the runtime; no browser process can start.
    runtime = MagicMock()
    runtime.__enter__.return_value.chromium.launch.side_effect = PlaywrightTimeout("startup")
    monkeypatch.setattr(
        "travel_deal_agent.providers.rainbow_browser.sync_playwright", lambda: runtime
    )

    # Act / assert.
    with (
        pytest.raises(RainbowTimeout, match="startup"),
        open_listing(SearchPlan.from_filters(settings.filters), Limits()),
    ):
        pytest.fail("A failed startup must not yield a listing")
    runtime.__exit__.assert_called_once()


DETAIL_URL = "https://r.pl/turcja-riwiera-wczasy/gardenia-hotel"


def evidence() -> ListingEvidence:
    return ListingEvidence(
        "PRODUCT",
        "OPAQUE",
        DETAIL_URL,
        (),
        date(2026, 12, 5),
        8,
        7,
        Decimal("1551"),
        ("KTW",),
        ("HB",),
    )


def test_evidence_returns_none_for_an_empty_listing() -> None:
    # Arrange.
    listing = BrowserListing(cast(Page, page_mock()), Limits())
    listing.empty = True

    # Act / assert.
    assert listing.evidence(cast(Offer, MagicMock())) is None


def test_evidence_returns_none_while_capture_is_pending_or_disabled() -> None:
    # Arrange.
    listing = BrowserListing(cast(Page, page_mock()), Limits())
    listing.capture = MagicMock(pending=True, disabled=False, exchanges=[])

    # Act / assert.
    assert listing.evidence(cast(Offer, MagicMock())) is None


def test_evidence_records_the_url_it_authorizes_for_detail_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange.
    listing = BrowserListing(cast(Page, page_mock()), Limits())
    listing.capture = MagicMock(pending=False, disabled=False, exchanges=[])
    result = evidence()
    monkeypatch.setattr(
        "travel_deal_agent.providers.rainbow_browser.evidence_for", lambda *a, **k: result
    )

    # Act.
    found = listing.evidence(cast(Offer, MagicMock()))

    # Assert: only an evidenced URL is later allowed as a detail request.
    assert found is result
    assert DETAIL_URL in listing.allowed_details


def test_detail_html_rejects_a_url_without_matching_listing_evidence() -> None:
    # Arrange.
    listing = BrowserListing(cast(Page, page_mock()), Limits())

    # Act / assert.
    with pytest.raises(RainbowDetailError, match="lacks matching listing evidence"):
        listing.detail_html(DETAIL_URL)


def test_detail_html_rejects_an_exhausted_budget() -> None:
    # Arrange.
    listing = BrowserListing(cast(Page, page_mock()), Limits(max_detail_requests=0))
    listing.allowed_details.add(DETAIL_URL)

    # Act / assert.
    with pytest.raises(RainbowDetailError, match="budget exhausted"):
        listing.detail_html(DETAIL_URL)


def detail_page_mock(status: int = 200, body: bytes = b"hello", url: str = DETAIL_URL) -> MagicMock:
    detail = MagicMock()
    response = MagicMock()
    response.status = status
    response.headers = {"content-length": str(len(body))}
    response.body.return_value = body
    detail.goto.return_value = response
    detail.url = url
    detail.locator.return_value.inner_text.return_value = "Rainbow"
    return detail


def test_detail_html_returns_the_decoded_document_on_success() -> None:
    # Arrange.
    page = page_mock()
    detail = detail_page_mock()
    page.context.new_page.return_value = detail
    listing = BrowserListing(cast(Page, page), Limits())
    listing.allowed_details.add(DETAIL_URL)

    # Act.
    result = listing.detail_html(DETAIL_URL)

    # Assert.
    assert result == "hello"
    detail.close.assert_called_once()


def test_detail_html_rejects_blocked_status() -> None:
    # Arrange.
    page = page_mock()
    detail = detail_page_mock(status=403)
    page.context.new_page.return_value = detail
    listing = BrowserListing(cast(Page, page), Limits())
    listing.allowed_details.add(DETAIL_URL)

    # Act / assert.
    with pytest.raises(RainbowBlocked):
        listing.detail_html(DETAIL_URL)
    detail.close.assert_called_once()


def test_detail_html_rejects_a_redirected_document() -> None:
    # Arrange.
    page = page_mock()
    detail = detail_page_mock(url="https://r.pl/turcja-riwiera-wczasy/other-hotel")
    page.context.new_page.return_value = detail
    listing = BrowserListing(cast(Page, page), Limits())
    listing.allowed_details.add(DETAIL_URL)

    # Act / assert.
    with pytest.raises(RainbowDetailError, match="unavailable or redirected"):
        listing.detail_html(DETAIL_URL)


def test_detail_html_rejects_an_oversized_document() -> None:
    # Arrange.
    page = page_mock()
    detail = detail_page_mock()
    detail.goto.return_value.headers = {"content-length": str(MAX_HTML_BYTES + 1)}
    page.context.new_page.return_value = detail
    listing = BrowserListing(cast(Page, page), Limits())
    listing.allowed_details.add(DETAIL_URL)

    # Act / assert.
    with pytest.raises(RainbowDetailError, match="exceeds size limit"):
        listing.detail_html(DETAIL_URL)


def test_detail_html_route_only_allows_the_exact_navigation_document() -> None:
    # Arrange.
    page = page_mock()
    detail = detail_page_mock()
    page.context.new_page.return_value = detail
    listing = BrowserListing(cast(Page, page), Limits())
    listing.allowed_details.add(DETAIL_URL)

    # Act.
    listing.detail_html(DETAIL_URL)

    # Assert: only the exact requested navigation document is let through.
    handler = detail.route.call_args.args[1]
    allowed = MagicMock()
    allowed.request.is_navigation_request.return_value = True
    allowed.request.frame = detail.main_frame
    allowed.request.method = "GET"
    allowed.request.url = DETAIL_URL
    allowed.request.redirected_from = None
    handler(allowed)
    allowed.continue_.assert_called_once()
    allowed.abort.assert_not_called()

    other = MagicMock()
    other.request.is_navigation_request.return_value = False
    handler(other)
    other.abort.assert_called_once()
    other.continue_.assert_not_called()
