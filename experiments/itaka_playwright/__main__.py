"""Bounded, single-session ITAKA search with optional manual detail inspection.

No scheduler, production provider, external ratings or notifier is involved.
Startup applies the recorded search flow; there is no crawl or retry loop.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Locator, Page, Request, expect, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from experiments.itaka_playwright.listing import (
    BrowserCards,
    collect_candidates,
    qualifies,
    read_card,
)
from experiments.itaka_playwright.settings import (
    SearchFilters,
    load_business_filters,
    load_search_settings,
)


def set_duration_7_9(page: Page, checked: bool = True) -> None:
    """Click the visible label, then verify the associated filter's checked state."""
    dialog = page.get_by_role("dialog")
    checkbox = dialog.get_by_label("7-9 dni", exact=True)
    label = dialog.get_by_text("7-9 dni", exact=True)
    expect(label).to_be_visible()
    if checkbox.is_checked() != checked:
        label.click()
    expect(checkbox).to_be_checked(checked=checked)


def prepare_search(page: Page) -> None:
    """Use the observed fresh-session defaults, failing if the party has changed."""
    page.get_by_role("button", name="Ile osób 2 os., 1 pokój", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text(re.compile(r"Liczba pokoi\s*1(?!\d)"))
    expect(dialog).to_contain_text(re.compile(r"Dorośli\s*2(?!\d)"))
    expect(dialog).to_contain_text(re.compile(r"Dzieci \(0-17 lat\)\s*0(?!\d)"))
    dialog.get_by_role("button", name="Zapisz", exact=True).click()
    # A fresh context and this exact summary ensure no departure dates were selected.
    page.get_by_role("button", name="Kiedy i na ile dowolnie", exact=True).click()
    set_duration_7_9(page)
    dialog.get_by_role("button", name="Zapisz", exact=True).click()
    page.get_by_role("button", name="Skąd i jak Dowolnie", exact=True).click()
    # The first observed expander belongs to airports, the second to coach cities.
    dialog.get_by_role("button", name="rozwiń", exact=True).first.click()
    for city in ("Warszawa", "Katowice", "Wrocław", "Łódź"):
        checkbox = dialog.get_by_role("checkbox", name=city, exact=True)
        checkbox.check()
        expect(checkbox).to_be_checked()
    dialog.get_by_role("button", name="Zapisz", exact=True).click()
    page.get_by_role("button", name="Szukaj", exact=True).click()


def set_meal_filters(page: Page) -> None:
    """Use the supplied nested labels; hidden inputs only expose checked state."""
    panel = page.locator("#modal-offcanvas-container")
    heading = panel.get_by_role("heading", level=5, name=re.compile(r"^Wyżywienie$", re.I))
    expect(heading).to_be_visible()
    # Only the list in this heading's own section, not other lists in the panel.
    meals = heading.locator("..").locator(":scope > ul")
    names = {
        "A": "All inclusive",
        "V": "3 posiłki",
        "H": "2 posiłki",
        "F": "Śniadania",
        "U": "Bez wyżywienia",
        "X": "Wyżywienie zgodnie z programem",
    }
    for code, name in names.items():
        label = (
            meals.locator("label")
            .filter(has=page.locator(f'input[type="checkbox"][id="{code}"]'))
            .filter(has_text=re.compile(rf"^\s*{re.escape(name)}\s*$"))
        )
        checkbox = label.locator(f'input[type="checkbox"][id="{code}"]')
        expect(label).to_be_visible()
        selected = code in ("A", "V", "H")
        if checkbox.is_checked() != selected:
            label.click()
        expect(checkbox).to_be_checked(checked=selected)


def apply_listing_filters(page: Page, filters: SearchFilters) -> None:
    """Apply the Codegen controls before any bounded listing extraction."""
    sort = page.get_by_role("button", name=re.compile(r"^Sortuj:"))
    sort.click()
    page.get_by_role("button", name="Najniższa cena", exact=True).click()
    expect(sort).to_contain_text("Najniższa cena")
    page.get_by_role("button", name=re.compile(r"(?:^|\s)Filtry$")).click()
    # Click the visible associated label; the native radio can be hidden.
    stars = page.locator(f'input[name="hotelRating"][value="{filters.min_stars}"]')
    label = page.locator(f'label[for="hotelRating-{filters.min_stars}"]')
    expect(label).to_be_visible()
    label.click()
    expect(stars).to_be_checked()
    price = page.locator('input[name="priceTo"]')
    value = format(filters.max_price, "f")
    price.fill(value)
    expect(price).to_have_value(value)
    set_meal_filters(page)
    apply = page.get_by_role("button", name="Pokaż oferty", exact=True)
    apply.click()
    expect(apply).not_to_be_visible()
    expect(sort).to_contain_text("Najniższa cena")


def wait_for_listing(page: Page, filters: SearchFilters, max_offers: int) -> None:
    """Verify observed URL parameters and wait for a quiet bounded card snapshot."""
    if max_offers < 1:
        raise ValueError("Offer limit must be positive")
    # These query keys were captured in the previous live run's request log.
    for key, value in (
        ("order", "priceAsc"),
        ("hotelRating", str(filters.min_stars)),
        ("priceTo", format(filters.max_price, "f")),
    ):
        expect(page).to_have_url(re.compile(rf"[?&]{key}={re.escape(value)}(?:&|$)"))
    page.get_by_test_id("offer-list-item").first.wait_for(state="visible")
    deadline = monotonic() + 15
    unchanged_since = monotonic()
    previous: tuple[str, list[dict[str, str | None]]] | None = None
    while monotonic() < deadline:
        current = (page.url, read_offers(page, max_offers))
        if current != previous or not current[1]:
            previous = current
            unchanged_since = monotonic()
        elif monotonic() - unchanged_since >= 1:
            # A changing URL must not silently invalidate the earlier checks.
            query = parse_qs(urlsplit(page.url).query)
            if (
                query.get("order") != ["priceAsc"]
                or query.get("hotelRating") != [str(filters.min_stars)]
                or query.get("priceTo") != [format(filters.max_price, "f")]
            ):
                raise ValueError("Listing URL changed after applying filters")
            return
        page.wait_for_timeout(200)
    raise PlaywrightTimeoutError("First N listing cards did not stabilize within 15000ms")


def open_offer_detail(page: Page, index: int = 0, *, max_offers: int) -> Page:
    """Open one of the configured first N offers and return its detail tab."""
    if not 0 <= index < max_offers:
        raise ValueError("Offer index exceeds configured limit")
    with page.expect_popup() as page1_info:
        page.get_by_test_id("offer-list-item-button").nth(index).click()
    page1 = page1_info.value
    page1.wait_for_load_state("domcontentloaded")
    return page1


def read_offers(page: Page, max_offers: int) -> list[dict[str, str | None]]:
    """Read at most N cards in UI order, without pagination or scrolling."""
    if max_offers < 1:
        raise ValueError("Offer limit must be positive")
    cards = page.get_by_test_id("offer-list-item")
    return [read_card(cards.nth(i)) for i in range(min(max_offers, cards.count()))]


def main() -> None:
    filters, limits = load_search_settings()
    business_filters = load_business_filters()
    print(
        f"Requested: max {filters.max_price} PLN/person, min {filters.min_stars} stars, "
        f"ascending price, target {limits.max_offers} candidates, "
        f"at most {limits.max_analyzed_cards} analyzed cards",
        flush=True,
    )
    output = Path("data/itaka-playwright") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output.mkdir(parents=True)
    requests: list[dict[str, object]] = []
    commands: list[dict[str, object]] = []
    detail_clicks = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(locale="pl-PL", viewport={"width": 1440, "height": 1000})
        context.set_default_timeout(15000)
        page = context.new_page()

        def record(request: Request) -> None:
            requests.append(
                {
                    "url": request.url,
                    "method": request.method,
                    "type": request.resource_type,
                    "navigation": request.is_navigation_request(),
                }
            )

        context.on("request", record)

        def snapshot(label: str) -> None:
            content = page.locator("body").aria_snapshot()
            (output / f"{label}.txt").write_text(content, encoding="utf-8")
            page.screenshot(path=str(output / f"{label}.png"), full_page=True)
            (output / f"{label}.html").write_text(page.content(), encoding="utf-8")
            print(
                json.dumps(
                    {"url": page.url, "requests": len(requests), "snapshot": content},
                    ensure_ascii=True,
                ),
                flush=True,
            )

        def locator(command: dict[str, object]) -> Locator:
            name = str(command["name"])
            role = command.get("role")
            if role is not None:
                if role not in ("button", "checkbox", "radio", "link", "textbox", "tab"):
                    raise ValueError("Unsupported role")
                target = page.get_by_role(role, name=name, exact=True)
            else:
                target = page.get_by_text(name, exact=True)
            if "index" in command:
                target = target.nth(int(str(command["index"])))
            return target

        try:
            page.goto("https://www.itaka.pl/", wait_until="domcontentloaded", timeout=45000)
            # The consent dialog can arrive after DOMContentLoaded. Use its observed label.
            consent = page.get_by_role("button", name="Akceptuję wszystkie", exact=True)
            try:
                consent.wait_for(state="visible", timeout=10000)
            except PlaywrightTimeoutError:
                pass
            else:
                consent.click()
            snapshot("00-home")
            prepare_search(page)
            apply_listing_filters(page, filters)
            wait_for_listing(page, filters, limits.max_offers)
            today = datetime.now(timezone.utc).date()
            collection = collect_candidates(
                BrowserCards(page, limits),
                limits,
                lambda card: qualifies(card, business_filters, today),
            )
            offers = collection.candidates
            (output / "collection.json").write_text(
                json.dumps(
                    {
                        "analyzed": collection.analyzed,
                        "scrolls": collection.scrolls,
                        "candidates": len(offers),
                        "stop_reason": collection.stop_reason,
                    }
                ),
                encoding="utf-8",
            )
            (output / "offers.json").write_text(
                json.dumps(offers, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(offers, ensure_ascii=True), flush=True)
            snapshot("01-filtered-listing")
            # Bounded manual checkpoints in one session, not repeated search attempts.
            for step in range(1, 31):
                line = sys.stdin.readline()
                if not line:
                    break
                command: dict[str, object] = json.loads(line)
                commands.append(command)
                action = command.get("action")
                if action == "finish":
                    break
                if action == "click":
                    if command.get("name") == "Sprawdź ofertę":
                        if not str(command.get("missing_data", "")).strip():
                            raise ValueError("Name the missing listing data before opening details")
                        if detail_clicks:
                            raise ValueError("Only one detail opening is allowed")
                        detail_clicks += 1
                        page = open_offer_detail(
                            page, int(str(command.get("index", 0))), max_offers=limits.max_offers
                        )
                    else:
                        locator(command).click()
                elif action == "check":
                    checked = bool(command.get("checked", True))
                    if command.get("role") == "checkbox" and command.get("name") == "7-9 dni":
                        set_duration_7_9(page, checked)
                    else:
                        locator(command).set_checked(checked)
                elif action == "snapshot":
                    pass
                elif action == "offers":
                    print(json.dumps(read_offers(page, limits.max_offers)), flush=True)
                elif action == "links":
                    links = page.get_by_test_id("offer-list-item-button")
                    print(
                        json.dumps(
                            [
                                {
                                    "text": links.nth(i).inner_text(),
                                    "url": links.nth(i).get_attribute("href"),
                                }
                                for i in range(min(limits.max_offers, links.count()))
                            ],
                            ensure_ascii=True,
                        ),
                        flush=True,
                    )
                else:
                    raise ValueError("Unsupported action")
                snapshot(f"{step:02d}")
        except Exception as exc:
            print(
                json.dumps({"stopped": type(exc).__name__, "error": str(exc)}, ensure_ascii=True),
                flush=True,
            )
            (output / "error.txt").write_text(str(exc), encoding="utf-8")
            raise
        finally:
            (output / "requests.json").write_text(json.dumps(requests, indent=2), encoding="utf-8")
            (output / "commands.json").write_text(json.dumps(commands, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {
                        "output": str(output),
                        "request_count": len(requests),
                        "document_requests": sum(r["type"] == "document" for r in requests),
                        "detail_clicks": detail_clicks,
                    }
                ),
                flush=True,
            )
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
