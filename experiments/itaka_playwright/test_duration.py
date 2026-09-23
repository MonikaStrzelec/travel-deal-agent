"""Offline Chromium checks for ITAKA's hidden input and visible duration label."""

import pytest
from playwright.sync_api import Route, expect, sync_playwright

from experiments.itaka_playwright.__main__ import set_duration_7_9


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("desired", [False, True])
def test_duration_label_changes_only_the_associated_filter(initial: bool, desired: bool) -> None:
    # Arrange: the label/input/span structure comes from the saved ITAKA dialog.
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(offline=True)
            attempted_urls: list[str] = []

            def block_request(route: Route) -> None:
                attempted_urls.append(route.request.url)
                route.abort()

            context.route("**/*", block_request)
            page = context.new_page()
            page.set_content(
                "<style>input {display:none} input:checked + span {color:rgb(255,0,0)}</style>"
                '<div role="dialog"><label for="duration-4,6">'
                '<input id="duration-4,6" type="checkbox"><span>4-6 dni</span></label>'
                '<label for="duration-7,9"><input id="duration-7,9" type="checkbox" '
                + ("checked" if initial else "")
                + "><span>7-9 dni</span><span></span></label></div>"
            )
            checkbox = page.get_by_label("7-9 dni", exact=True)
            expect(checkbox).not_to_be_visible()
            # Act
            set_duration_7_9(page, desired)
            # Assert: the hidden control and its visible presentation agree.
            expect(checkbox).to_be_checked(checked=desired)
            expect(page.get_by_text("7-9 dni", exact=True)).to_have_css(
                "color", "rgb(255, 0, 0)" if desired else "rgb(0, 0, 0)"
            )
            expect(page.get_by_label("4-6 dni", exact=True)).not_to_be_checked()
            assert attempted_urls == []
        finally:
            browser.close()
