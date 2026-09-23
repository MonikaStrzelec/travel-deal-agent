"""Offline semantic controls based on Codegen and the saved accessibility snapshots."""

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from playwright.sync_api import Page, Route, expect, sync_playwright

from experiments.itaka_playwright.__main__ import (
    apply_listing_filters,
    prepare_search,
    read_offers,
    set_meal_filters,
    wait_for_listing,
)
from experiments.itaka_playwright.listing import BrowserCards, collect_candidates
from experiments.itaka_playwright.settings import PocLimits, SearchFilters


def meal_html(checked: str = "", hidden: bool = True) -> str:
    """Independent fixture based on the user-supplied meal DOM."""
    names = {
        "A": "All inclusive",
        "V": "3 posiłki",
        "H": "2 posiłki",
        "F": "Śniadania",
        "U": "Bez wyżywienia",
        "X": "Wyżywienie zgodnie z programem",
    }
    return (
        '<div id="modal-offcanvas-container"><section><h5>Wyżywienie</h5><ul>'
        + "".join(
            f'<li><label><input id="{code}" type="checkbox" '
            f'style="display:{"none" if hidden else "inline"}" '
            f"{'checked' if code in checked else ''}><span></span><span>{name}</span></label></li>"
            for code, name in names.items()
        )
        + "</ul></section></div>"
    )


@pytest.mark.parametrize("checked", ["", "AVH", "AVHFUX", "FUX"])
@pytest.mark.parametrize("hidden", [True, False])
def test_meals_select_exactly_three_options(page: Page, checked: str, hidden: bool) -> None:
    # Arrange
    page.set_content(meal_html(checked, hidden))
    # Act: repeated application must not toggle selected options off.
    set_meal_filters(page)
    set_meal_filters(page)
    # Assert
    for code in "AVHFUX":
        expect(page.locator(f'input[id="{code}"]')).to_be_checked(checked=code in "AVH")
        if hidden:
            expect(page.locator(f'input[id="{code}"]')).not_to_be_visible()


@pytest.fixture
def page() -> Iterator[Page]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(offline=True)
        attempts: list[str] = []

        def block(route: Route) -> None:
            attempts.append(route.request.url)
            route.abort()

        context.route("http://**/*", block)
        context.route("https://**/*", block)
        try:
            yield context.new_page()
            assert attempts == []
        finally:
            browser.close()


@pytest.mark.parametrize("other_checked", [False, True])
def test_meals_ignore_duplicate_id_in_other_section(page: Page, other_checked: bool) -> None:
    # Arrange: even identical label text outside the meal section must not be selected.
    other = (
        '<section aria-label="Other"><h5>Other</h5><ul><li><label>'
        '<input id="A" type="checkbox" style="display:none" '
        + ("checked" if other_checked else "")
        + "><span>All inclusive</span></label></li></ul></section>"
    )
    page.set_content(
        meal_html().replace("<section><h5>Wyżywienie</h5>", other + "<section><h5>WYŻYWIENIE</h5>")
    )
    expect(page.locator('input[id="A"]')).to_have_count(2)
    # Act
    set_meal_filters(page)
    # Assert: only the requested section changes, including with hidden inputs.
    expect(page.get_by_role("region", name="Other").locator("input")).to_be_checked(
        checked=other_checked
    )
    meals = page.get_by_role("heading", name="WYŻYWIENIE", exact=True).locator("..")
    for code in "AVHFUX":
        expect(meals.locator(f'input[id="{code}"]')).to_be_checked(checked=code in "AVH")


@pytest.mark.parametrize("suffix", ["", "★", " ⭐", " \ue810"])
@pytest.mark.parametrize("budget,minimum", [("1500", 3), ("1234.50", 4)])
@pytest.mark.parametrize("hidden_radio", [False, True])
def test_sort_and_apply_filters_without_reopening(
    page: Page, suffix: str, budget: str, minimum: int, hidden_radio: bool
) -> None:
    # Arrange: unrelated inputs/options must not be touched.
    page.set_content(
        '<button id="sort" onclick="options.hidden=false">Sortuj: Rekomendowane dla Ciebie</button>'
        '<section id="options" hidden><button onclick="'
        "document.getElementById('sort').textContent='Sortuj: Najniższa cena';"
        'options.hidden=true">Najniższa cena</button></section>'
        '<button id="filters" onclick="panel.hidden=false; '
        'this.dataset.opens=Number(this.dataset.opens || 0)+1">&#xE816; Filtry</button>'
        '<section id="panel" hidden>'
        '<label><input type="radio" name="hotelRating" value="30">Od 30</label>'
        f'<input type="radio" name="hotelRating" value="{minimum}" '
        f'id="hotelRating-{minimum}" style="display:{"none" if hidden_radio else "inline"}">'
        f'<label for="hotelRating-{minimum}"><span>Od {minimum}{suffix}</span></label>'
        '<input name="priceFrom" value="0"><input name="priceTo">'
        + meal_html()
        + '<button onclick="panel.hidden=true; '
        "document.getElementById('filters').textContent='Filtry (2)'"
        '">Pokaż oferty</button></section>'
    )
    filters = SearchFilters(max_price=Decimal(budget), min_stars=minimum, currency="PLN")
    # Act
    apply_listing_filters(page, filters)
    # Assert
    expect(page.get_by_role("button", name="Sortuj: Najniższa cena")).to_be_visible()
    expect(page.locator('input[name="priceTo"]')).to_have_value(budget)
    expect(page.locator('input[name="priceFrom"]')).to_have_value("0")
    expect(page.locator("#filters")).to_have_attribute("data-opens", "1")
    expect(page.get_by_role("button", name="Pokaż oferty")).not_to_be_visible()
    expect(page.locator('input[name="hotelRating"][value="30"]')).not_to_be_checked()
    radio = page.locator(f'input[name="hotelRating"][value="{minimum}"]')
    expect(radio).to_be_checked()
    for code in "AVHFUX":
        expect(page.locator(f'input[id="{code}"]')).to_be_checked(checked=code in "AVH")
    if hidden_radio:
        expect(radio).not_to_be_visible()


def test_listing_waits_for_delayed_card_update_without_opening_panels(
    page: Page, tmp_path: Path
) -> None:
    # Arrange: a local URL carries the observed parameters; cards change asynchronously.
    listing = tmp_path / "listing.html"
    listing.write_text(
        "<button disabled>Filtry (2)</button>"
        '<article data-testid="offer-list-item"><h3>Loading</h3>'
        '<a data-testid="offer-list-item-button" href="/old">Offer</a></article>'
        '<article data-testid="offer-list-item"><h3>Outside limit</h3>'
        '<a data-testid="offer-list-item-button" href="/extra">Offer</a></article>',
        encoding="utf-8",
    )
    page.goto(listing.as_uri() + "?hotelRating=3&priceTo=1500&order=priceAsc")
    page.evaluate("""() => {
        setTimeout(() => {
            document.querySelector('h3').textContent = 'Ready Hotel';
            document.querySelector('a').setAttribute('href', '/ready');
        }, 400);
    }""")
    filters = SearchFilters(max_price=Decimal("1500"), min_stars=3, currency="PLN")
    # Act
    wait_for_listing(page, filters, 1)
    offers = read_offers(page, 1)
    # Assert
    assert len(offers) == 1
    assert offers[0]["hotel_name"] == "Ready Hotel"
    assert offers[0]["url"] == "/ready"
    assert len(page.context.pages) == 1


def test_fresh_search_preserves_party_dates_duration_and_airports(page: Page) -> None:
    # Arrange: hidden duration input and separate airport/coach expanders.
    page.set_content(
        '<button onclick="party.hidden=false">Ile osób 2 os., 1 pokój</button>'
        '<div role="dialog" id="party" hidden>Liczba pokoi 1 Dorośli 2 Dzieci (0-17 lat) 0'
        '<button onclick="party.hidden=true">Zapisz</button></div>'
        '<button onclick="duration.hidden=false">Kiedy i na ile dowolnie</button>'
        '<div role="dialog" id="duration" hidden>'
        '<input type="date" aria-label="Departure date">'
        '<label><input style="display:none" type="checkbox">7-9 dni</label>'
        '<button onclick="duration.hidden=true">Zapisz</button></div>'
        '<button onclick="airports.hidden=false">Skąd i jak Dowolnie</button>'
        '<div role="dialog" id="airports" hidden>'
        '<button onclick="cities.hidden=false">rozwiń</button><section id="cities" hidden>'
        + "".join(
            f'<label><input type="checkbox">{city}</label>'
            for city in ("Warszawa", "Katowice", "Wrocław", "Łódź", "Poznań")
        )
        + "</section><button onclick=\"throw Error('Wrong expander')\">rozwiń</button>"
        '<button onclick="airports.hidden=true">Zapisz</button></div>'
        "<button onclick=\"this.textContent='Search submitted'\">Szukaj</button>"
    )
    # Act
    prepare_search(page)
    # Assert
    expect(page.get_by_role("button", name="Search submitted")).to_be_visible()
    expect(page.locator('input[type="date"]')).to_have_value("")
    page.get_by_role("button", name="Skąd i jak Dowolnie").click()
    for city in ("Warszawa", "Katowice", "Wrocław", "Łódź"):
        expect(page.get_by_role("checkbox", name=city, exact=True)).to_be_checked()
    expect(page.get_by_role("checkbox", name="Poznań")).not_to_be_checked()


def test_listing_fields_without_detail_navigation(page: Page) -> None:
    # Arrange: selectors/roles mirror the saved card; no production data dependency.
    page.set_content(
        '<article data-testid="offer-list-item"><h3>Example Hotel</h3>'
        '<div data-testid="offer-list-item-destination">Grecja, Kreta</div>'
        '<span data-testid="reviews-rating">5.3/6</span><span>123 opinii</span>'
        '<span data-testid="rating-stars" role="list">'
        '<i role="listitem"></i><i role="listitem"></i><i role="listitem"></i></span>'
        "<div>30.11 - 7.12.2026 (8 dni) Katowice 12:00 2 posiłki</div>"
        '<div data-testid="current-price">1 234 zł /os.</div><span>+30 zł (TFG i TFP)</span>'
        '<a data-testid="offer-list-item-button" href="/variant?id=A" target="_blank">'
        "Sprawdź ofertę</a></article>"
    )
    # Act
    offers = read_offers(page, 10)
    # Assert
    assert len(offers) == 1
    assert offers[0]["hotel_name"] == "Example Hotel"
    assert offers[0]["destination"] == "Grecja, Kreta"
    assert offers[0]["rating_text"] == "5.3/6"
    assert offers[0]["star_icons"] == "3"
    assert offers[0]["price_text"] == "1 234 zł /os."
    assert offers[0]["url"] == "/variant?id=A"
    assert "123 opinii" in (offers[0]["text"] or "")
    assert "+30 zł" in (offers[0]["text"] or "")
    assert len(page.context.pages) == 1


def test_browser_scroll_collects_only_new_variants_without_navigation(page: Page) -> None:
    # Arrange: emulate lazy cards locally; even same-hotel variants have distinct URLs.
    page.set_content("""
        <article data-testid="offer-list-item">RO
          <a data-testid="offer-list-item-button" href="/hotel?id=0">Offer</a>
        </article>
        <script>
        window.addEventListener('wheel', () => {
          for (const id of [1, 2, 3]) {
            const card = document.createElement('article');
            card.setAttribute('data-testid', 'offer-list-item');
            card.innerHTML = 'HB <a data-testid="offer-list-item-button" '
                + 'href="/hotel?id=' + id + '">Offer</a>';
            document.body.appendChild(card);
          }
        }, {once: true});
        </script>
    """)
    limits = PocLimits(max_offers=2, scroll_wait_ms=200)
    # Act
    result = collect_candidates(
        BrowserCards(page, limits), limits, lambda card: "HB" in (card["text"] or "")
    )
    # Assert
    assert [card["url"] for card in result.candidates] == ["/hotel?id=1", "/hotel?id=2"]
    assert result.analyzed == 3
    assert result.scrolls == 1
    assert len(page.context.pages) == 1
    assert page.url == "about:blank"
