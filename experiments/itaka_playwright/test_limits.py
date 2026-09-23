"""Offline configuration and bounded listing regression tests."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from experiments.itaka_playwright.__main__ import read_offers
from experiments.itaka_playwright.settings import (
    load_search_settings,
)


def test_configured_budget_and_limit(tmp_path: Path) -> None:
    business = tmp_path / "config.json"
    poc = tmp_path / "poc.json"
    business.write_text(
        json.dumps(
            {
                "filters": {
                    "max_price": "1234.50",
                    "min_stars": 4,
                    "currency": "PLN",
                }
            }
        ),
        encoding="utf-8",
    )
    poc.write_text('{"max_offers": 2}', encoding="utf-8")
    filters, limits = load_search_settings(business, poc)
    assert filters.max_price == Decimal("1234.50")
    assert filters.min_stars == 4
    assert limits.max_offers == 2
    poc.write_text('{"max_offers": 0}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_search_settings(business, poc)


def test_repository_search_defaults() -> None:
    filters, limits = load_search_settings()
    assert filters.max_price == Decimal("1500")
    assert filters.min_stars == 3
    assert limits.max_offers == 10
    assert limits.max_analyzed_cards == 15


def test_reads_only_first_n_cards_in_ui_order() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(offline=True)
            page = context.new_page()
            page.set_content(
                "".join(
                    '<article data-testid="offer-list-item">'
                    f'<a data-testid="offer-list-item-button" href="/offer/{i}">Hotel {i}</a>'
                    "</article>"
                    for i in range(4)
                )
            )
            assert [offer["url"] for offer in read_offers(page, 2)] == ["/offer/0", "/offer/1"]
            assert len(read_offers(page, 10)) == 4
            assert read_offers(page, 1)[0]["price_text"] is None
            assert len(context.pages) == 1
            assert page.url == "about:blank"
        finally:
            browser.close()
