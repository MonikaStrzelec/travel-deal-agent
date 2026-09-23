"""Offline popup checks using local HTML, without requests to ITAKA."""

from pathlib import Path

import pytest
from playwright.sync_api import Route, expect, sync_playwright

from experiments.itaka_playwright.__main__ import open_offer_detail


def test_detail_analysis_uses_popup_and_preserves_selected_variant(tmp_path: Path) -> None:
    # Arrange: two distinct variants, opened through the observed stable test ID.
    detail = tmp_path / "detail.html"
    detail.write_text("<h1>Variant A</h1><p>2 adults, 7 nights</p>", encoding="utf-8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(offline=True)
            attempted_urls: list[str] = []

            def block_request(route: Route) -> None:
                attempted_urls.append(route.request.url)
                route.abort()

            context.route("https://**/*", block_request)
            context.route("http://**/*", block_request)
            page = context.new_page()
            listing = tmp_path / "listing.html"
            listing.write_text(
                '<h1>Listing</h1><a data-testid="offer-list-item-button" '
                f'target="_blank" href="{detail.as_uri()}?rateId=A">Sprawdź ofertę</a>'
                '<a data-testid="offer-list-item-button" '
                f'target="_blank" href="{detail.as_uri()}?rateId=B">Sprawdź ofertę</a>',
                encoding="utf-8",
            )
            page.goto(listing.as_uri())
            # Act
            page1 = open_offer_detail(page, max_offers=2)
            # Assert: subsequent analysis reads the popup, not the listing.
            assert page1 is not page
            assert page1.url.endswith("?rateId=A")
            expect(page1.get_by_role("heading", name="Variant A")).to_be_visible()
            assert "2 adults, 7 nights" in page1.locator("body").aria_snapshot()
            expect(page.get_by_role("heading", name="Listing")).to_be_visible()
            assert len(context.pages) == 2
            for invalid_index in (-1, 2):
                with pytest.raises(ValueError, match="configured limit"):
                    open_offer_detail(page, invalid_index, max_offers=2)
            assert len(context.pages) == 2
            assert attempted_urls == []
        finally:
            browser.close()
