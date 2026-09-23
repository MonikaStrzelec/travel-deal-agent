"""Bounded Playwright access using only the observed Rainbow search UI."""

import json
import logging
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, Route, expect, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ..models import Offer
from .rainbow_capture import ListingCapture
from .rainbow_config import (
    AIRPORT_LABELS,
    DURATIONS,
    MEALS,
    STAR_CODES,
    STAR_LABELS,
    Limits,
    SearchPlan,
)
from .rainbow_errors import RainbowBlocked, RainbowError, RainbowStructureError, RainbowTimeout
from .rainbow_listing_data import ListingEvidence, evidence_for, product_url
from .rainbow_nuxt import MAX_HTML_BYTES, RainbowDetailError

logger = logging.getLogger(__name__)
CARD = '[data-test-id^="r-bloczek:szukaj:"]'
EMPTY = '[data-test-id="r-typography:szukaj-brakWynikow:tytul"]'
SORT = '[name="szukaj-sortowanie"]'
COUNT = '[data-test-id="r-typography:szukaj-naglowek:liczbaOfert"]'

# State belongs to this wait only. Polling is condition-based, not a fixed sleep.
WAIT = """({expected, token}) => {
 const text=document.body?.innerText || '';
 if (/captcha|verify you are human|access denied|potwierdź.{0,30}człowiekiem/i.test(text))
   return 'blocked';
 const q=new URL(location.href).searchParams;
 if (!Object.entries(expected).every(([k,v]) =>
   JSON.stringify(q.getAll(k).sort())===JSON.stringify([...v].sort()))) return false;
 const root=document.querySelector('.szukaj-wyniki');
 if (!root || root.querySelector('.r-skeleton-bloczek-szukaj')) return false;
 const empty=root.querySelector('[data-test-id="r-typography:szukaj-brakWynikow:tytul"]');
 const isEmpty=empty?.textContent.includes('nie znaleźliśmy ofert');
 const cards=[...root.querySelectorAll('[data-test-id^="r-bloczek:szukaj:"]')];
 const count=root.querySelector('[data-test-id="r-typography:szukaj-naglowek:liczbaOfert"]');
 if (!isEmpty && (!cards.length || !count ||
   (expected.sortowanie &&
    document.querySelector('[name="szukaj-sortowanie"]')?.value!==expected.sortowanie[0]))) return false;
 const signature=JSON.stringify([location.href,isEmpty,count?.textContent,
   isEmpty ? [] : cards.slice(0,3).map(e=>e.textContent)]);
 const old=window.__tdaRainbowWait;
 if (!old || old.token!==token || old.signature!==signature) {
   window.__tdaRainbowWait={token,signature,since:performance.now()}; return false;
 }
 return performance.now()-old.since>=1000 ? (isEmpty ? 'empty' : 'ready') : false;
}"""


class Listing(Protocol):
    def count(self) -> int: ...
    def card_html(self, index: int) -> str: ...
    def advance(self) -> bool: ...
    def evidence(self, offer: Offer) -> ListingEvidence | None: ...
    def detail_html(self, url: str) -> str: ...


class BrowserListing:
    def __init__(
        self,
        page: Page,
        limits: Limits,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.page, self.limits, self.clock = page, limits, clock
        self.deadline = clock() + limits.cycle_seconds
        self.expected: dict[str, list[str]] = {"sortowanie": ["cena-asc"]}
        self.empty = False
        self.capture = ListingCapture(page)
        self.detail_requests = 0
        self.allowed_details: set[str] = set()

    def timeout(self) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise RainbowTimeout("Rainbow scan deadline exceeded")
        return min(remaining, self.limits.timeout_seconds) * 1000

    def guard(self) -> None:
        self.capture.check()
        self.page.set_default_timeout(self.timeout())
        text = self.page.locator("body").inner_text()
        if re.search(
            r"captcha|verify you are human|access denied|potwierdź.{0,30}człowiekiem", text, re.I
        ):
            raise RainbowBlocked("Rainbow requires human verification or denied access")

    def ready(self) -> None:
        result = self.page.wait_for_function(
            WAIT,
            arg={"expected": self.expected, "token": uuid4().hex},
            timeout=self.timeout(),
        )
        try:
            status = result.json_value()
        finally:
            result.dispose()
        if status == "blocked":
            raise RainbowBlocked("Rainbow requires human verification or denied access")
        self.empty = status == "empty"
        self.guard()

    def unique(self, locator: Locator) -> Locator:
        self.guard()
        # Sidebar controls can mount after the listing itself becomes ready.
        locator.wait_for(state="attached", timeout=self.timeout())
        if locator.count() != 1:
            raise RainbowStructureError("Missing or ambiguous Rainbow control")
        return locator

    def airport_panel(self, airports: tuple[str, ...]) -> Locator:
        """Wait for the opened panel and its asynchronously mounted controls."""
        panel = self.page.locator(".filtry-gorne-skad-wyjazd-tooltip")
        try:
            panel.wait_for(state="visible", timeout=self.timeout())
            group = panel.locator('[data-test-id="r-checkbox-group:filtryGorne:skad"]')
            group.wait_for(state="visible", timeout=self.timeout())
            expect(panel.locator('.r-skeleton:visible, [aria-busy="true"]:visible')).to_have_count(
                0, timeout=self.timeout()
            )
            for code in airports:
                control = group.get_by_role("checkbox", name=AIRPORT_LABELS[code], exact=True)
                control.wait_for(state="attached", timeout=self.timeout())
                expect(control).to_be_enabled(timeout=self.timeout())
            self.guard()
        except (PlaywrightTimeout, AssertionError) as exc:
            raise RainbowTimeout("Rainbow airport panel did not become ready") from exc
        return panel

    def check(self, control: Locator) -> None:
        target = self.unique(control)
        if not target.is_checked():
            identifier = target.get_attribute("id")
            if not identifier:
                raise RainbowStructureError("Rainbow control has no label ID")
            # JSON Unicode escapes (e.g. \u017c) are not CSS Unicode escapes.
            labels = self.page.locator(f"label[for={json.dumps(identifier, ensure_ascii=False)}]")
            if labels.count() != 1:
                raise RainbowStructureError("Rainbow checkbox label is ambiguous")
            labels.click()
        expect(target).to_be_checked(timeout=self.timeout())

    def fill(self, name: str, value: str) -> None:
        target = self.unique(self.page.get_by_role("spinbutton", name=name, exact=True))
        target.fill(value)
        target.blur()
        expect(target).to_have_value(value, timeout=self.timeout())

    def prepare(self, plan: SearchPlan) -> None:
        # Intermediate filter updates may reset sorting; enforce it after the final filter.
        self.expected.pop("sortowanie", None)
        response = self.page.goto(
            "https://r.pl/", wait_until="domcontentloaded", timeout=self.timeout()
        )
        if response and response.status in {403, 429}:
            raise RainbowBlocked(f"Rainbow HTTP {response.status}")
        if response and response.status >= 400:
            raise RainbowError(f"Rainbow HTTP {response.status}")
        self.guard()
        consent = self.page.get_by_role(
            "button", name="Akceptuj wszystkie pliki cookie", exact=True
        )
        consent.wait_for(state="visible", timeout=self.timeout())
        consent.click(timeout=self.timeout())
        self.unique(self.page.locator('[data-test-id="r-input-button:filtyGorne:skad"]')).click()
        panel = self.airport_panel(plan.airports)
        for code in plan.airports:
            self.check(panel.get_by_role("checkbox", name=AIRPORT_LABELS[code], exact=True))
        # Airport panel commits once, unlike the automatically applied sidebar controls.
        self.unique(self.page.locator('[data-test-id="r-button:filtyGorne:wybierz-skad"]')).click()
        self.unique(self.page.get_by_role("button", name="Szukaj", exact=True)).click()
        self.expected.update(
            {
                "wybraneSkad": list(plan.airports),
                "typTransportu": ["AIR"],
                "dzieci": ["nie"],
                "liczbaPokoi": ["1"],
                "data": [""],
                "dataWylotu": [""],
                "standardHotelu": [],
                "wyzywienia": [],
                "dlugoscPobytu": ["*-*"],
                "cena": ["avg"],
                "cena.od": [""],
                "cena.do": [""],
                "ocenaKlientow": ["*-*"],
                "odlegloscLotnisko": ["*-*"],
            }
        )
        self.ready()
        query = parse_qs(urlsplit(self.page.url).query, keep_blank_values=True)
        adults = query.get("dorosli", [])
        if len(adults) != 2 or any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in adults):
            raise RainbowStructureError("Rainbow party is not the observed two-adult default")
        self.expected["dorosli"] = adults
        for star in plan.stars:
            self.check(self.page.get_by_role("checkbox", name=STAR_LABELS[star], exact=True))
            self.expected["standardHotelu"].append(STAR_CODES[star])
            self.ready()
        self.check(
            self.page.get_by_role("radio", name=DURATIONS[plan.min_days, plan.max_days], exact=True)
        )
        self.expected["dlugoscPobytu"] = [f"{plan.min_days}-{plan.max_days}"]
        self.ready()
        meals = self.page.locator('[data-test-id="r-accordion:filtryBoczne:Wyżywienie"]')
        for board in plan.boards:
            label, value = MEALS[board]
            self.check(meals.get_by_role("checkbox", name=label, exact=True))
            self.expected["wyzywienia"].append(value)
            self.ready()
        if plan.rating_floor is not None:
            rating_control = self.unique(
                self.page.get_by_role(
                    "radio",
                    name=f"Od {plan.rating_floor}.0",
                    exact=True,
                )
            )
            rating_value = rating_control.get_attribute("value")
            if not rating_value:
                raise RainbowStructureError("Rainbow rating control has no value")
            self.check(rating_control)
            self.expected["ocenaKlientow"] = [rating_value]
            self.ready()
        self.check(self.page.get_by_role("radio", name="Cena za osobę", exact=True))
        self.expected["cena"] = ["avg"]
        self.ready()
        self.fill("Cena do", format(plan.max_price, "f"))
        self.expected["cena.do"] = [format(plan.max_price, "f")]
        self.ready()
        self.ensure_price_sort()

    def ensure_price_sort(self) -> None:
        """Finish preparation with verified ascending prices, never accept another order."""
        query = parse_qs(urlsplit(self.page.url).query)
        control = self.page.locator(SORT)
        if query.get("sortowanie") != ["cena-asc"] or (
            not self.empty and control.input_value() != "cena-asc"
        ):
            container = self.page.locator('[data-test-id="r-select-form:szukaj-sortowanie"]')
            self.unique(container.get_by_role("combobox")).click()
            self.unique(
                self.page.get_by_role("option", name="od najniższej ceny", exact=True)
            ).click()
        self.expected["sortowanie"] = ["cena-asc"]
        self.ready()

    def count(self) -> int:
        self.guard()
        if self.empty:
            return 0
        return self.page.locator(CARD).count()

    def card_html(self, index: int) -> str:
        self.guard()
        if self.empty:
            raise RainbowStructureError("Cannot read recommendation cards after empty results")
        # An enclosing product anchor is needed for its real href, not a constructed variant URL.
        card = self.page.locator(CARD).nth(index)
        return str(card.evaluate("el => el.closest('a')?.outerHTML || ''"))

    def advance(self) -> bool:
        before = self.count()
        if before == 0:
            return False
        self.page.locator(CARD).nth(before - 1).scroll_into_view_if_needed(timeout=self.timeout())
        self.ready()
        return self.count() > before

    def evidence(self, offer: Offer) -> ListingEvidence | None:
        self.guard()
        if self.empty:
            return None
        self.capture.synchronize(self.expected, self.timeout)
        self.guard()
        if self.capture.pending or self.capture.disabled:
            return None
        result = evidence_for(offer, self.capture.exchanges, self.expected)
        if result is not None:
            self.allowed_details.add(result.url)
        return result

    def detail_html(self, url: str) -> str:
        """One source-supplied document only; no scripts/assets/API or redirect follow-up."""
        self.guard()
        if url not in self.allowed_details or product_url(url) != url:
            raise RainbowDetailError("Detail URL lacks matching listing evidence")
        if self.detail_requests >= self.limits.max_detail_requests:
            raise RainbowDetailError("Rainbow detail request budget exhausted")
        self.detail_requests += 1
        detail = self.page.context.new_page()
        document_sent = False

        def document_only(route: Route) -> None:
            nonlocal document_sent
            request = route.request
            if (
                not document_sent
                and request.is_navigation_request()
                and request.frame == detail.main_frame
                and request.method == "GET"
                and request.url == url
                and request.redirected_from is None
            ):
                document_sent = True
                route.continue_()
            else:
                route.abort()

        try:
            detail.route("**/*", document_only)
            response = detail.goto(url, wait_until="domcontentloaded", timeout=self.timeout())
            self.timeout()
            if response is not None and response.status in {403, 429}:
                raise RainbowBlocked(f"Rainbow detail HTTP {response.status}")
            if response is None or response.status != 200 or detail.url != url:
                raise RainbowDetailError("Detail document unavailable or redirected")
            length = response.headers.get("content-length")
            if length is not None and (not length.isdigit() or int(length) > MAX_HTML_BYTES):
                raise RainbowDetailError("Detail document exceeds size limit")
            body = response.body()
            self.timeout()
            if len(body) > MAX_HTML_BYTES:
                raise RainbowDetailError("Detail document exceeds size limit")
            try:
                html = body.decode("utf-8")
            except UnicodeError as exc:
                raise RainbowDetailError("Invalid detail document encoding") from exc
            if re.search(
                r"captcha|verify you are human|access denied",
                detail.locator("body").inner_text(timeout=self.timeout()),
                re.I,
            ):
                raise RainbowBlocked("Rainbow detail requires human verification")
            return html
        finally:
            detail.close()


def save_diagnostics(page: Page, directory: Path) -> None:
    """One capped HTML snapshot and one viewport image; no per-card dumps."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        html = page.content().encode("utf-8")
        (directory / "page.html").write_bytes(html[:2_000_000])
        (directory / "url.txt").write_text(page.url, encoding="utf-8")
        page.screenshot(path=str(directory / "viewport.png"), timeout=3000)
    except (OSError, PlaywrightError):
        logger.warning("Could not save Rainbow diagnostics", exc_info=True)


@contextmanager
def _browser_listing(plan: SearchPlan, limits: Limits) -> Iterator[Listing]:
    from ..config import ROOT

    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, timeout=limits.timeout_seconds * 1000)
        try:
            context = browser.new_context(
                locale="pl-PL", viewport={"width": 1440, "height": 1000}, service_workers="block"
            )
            page = context.new_page()
            diagnostic_dir = ROOT / "data" / "rainbow-production" / uuid4().hex
            try:
                listing = BrowserListing(page, limits)
                listing.prepare(plan)
                try:
                    yield listing
                finally:
                    listing.capture.close()
                if limits.debug:
                    save_diagnostics(page, diagnostic_dir)
            except PlaywrightTimeout as exc:
                save_diagnostics(page, diagnostic_dir)
                raise RainbowTimeout("Rainbow browser operation timed out") from exc
            except PlaywrightError as exc:
                save_diagnostics(page, diagnostic_dir)
                raise RainbowError("Rainbow browser operation failed") from exc
            except RainbowError:
                save_diagnostics(page, diagnostic_dir)
                raise
            except AssertionError as exc:
                save_diagnostics(page, diagnostic_dir)
                raise RainbowStructureError("Rainbow control state verification failed") from exc
        finally:
            browser.close()


@contextmanager
def open_listing(plan: SearchPlan, limits: Limits) -> Iterator[Listing]:
    """Classify startup/cleanup failures as well as errors during page interaction."""
    try:
        with _browser_listing(plan, limits) as listing:
            yield listing
    except PlaywrightTimeout as exc:
        raise RainbowTimeout("Rainbow browser startup or cleanup timed out") from exc
    except PlaywrightError as exc:
        raise RainbowError("Rainbow browser startup or cleanup failed") from exc
