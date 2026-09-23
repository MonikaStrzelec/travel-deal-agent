# Rainbow browser experiment: offline filter-update contract

**Latest diagnostic:** [DIAGNOSTIC_2000.md](DIAGNOSTIC_2000.md) records one authorized
PLN 2000/person run, successful 3/4/5-star checkbox selection and extraction of three
cards. The PLN 1500 business cap is unchanged. Its confirmed star mapping supersedes
the earlier unknown-mapping notes below.

**Live evidence update (2026-09-20):** one authorized diagnostic session has now
completed with all requested filters and no matching offers. See
[LIVE_FINDINGS.md](LIVE_FINDINGS.md) for confirmed selectors, airport-panel behavior,
the empty-state wait limitation and remaining work. The recording-stage assumptions
below predate that run; the live findings take precedence where they differ.

An offline filter-update coordinator and tests are included in `search.py` and
`test_search.py`. No Rainbow browser adapter, scraper, live test or
production provider is included. The coordinator cannot run a live search by itself.
ITAKA code is a reference only and remains unchanged. Use the existing local Playwright
installation (currently pinned to 1.63.0 by the ITAKA experiment).

This experiment's tests are not part of the production `pytest -q` gate (see
README.md's "Tests and quality checks"); run them explicitly with
`pytest experiments/rainbow_playwright -q`.

## Confirmed manual behavior and POC settings

Each listing filter applies automatically and refreshes the results. There is no
separate "Pokaż oferty" or filter-submit step. Wait after **each** actual change,
including each airport and meal checkbox; do not batch clicks across refreshes.
The initial homepage search recorded by Codegen is not a listing-filter submit step.

`settings.json` records the confirmed POC selection (not yet loaded by a browser
adapter and not a replacement for production business rules):

- Łódź (LCJ), Katowice (KTW), Warszawa Chopin (WAW), Warszawa Modlin (WMI), Wrocław (WRO).
- 7–9 days.
- All inclusive, 3 posiłki, 2 posiłki, selected together.
- Maximum PLN 1500 per person and customer rating from 5.0 on Rainbow's native scale.
- Ascending price is the expected default: `sortowanie=cena-asc`.

Verify both the selected sorting UI and URL. If both confirm ascending price, do not
click sorting. The coordinator stops on disagreement/timeout; it does not guess a
sorting locator or perform an unverified correction. Do not infer that
`standardHotelu=6` means three stars. No hotel-standard mapping is implemented.

## Update completion contract

`change_filter` first verifies the current listing, captures its completed revision,
applies one change, and blocks until all of these conditions hold:

1. The URL matches the expected cumulative filter values and `sortowanie=cena-asc`.
2. The sorting UI confirms ascending price.
3. The listing is ready and has a new completed revision, including a completed
   empty-results state. A changed URL with old cards is insufficient.

`SearchPage.wait_until` must use a bounded condition-based browser wait. There is no
fixed sleep in the coordinator. Timeout propagates and prevents callers from continuing
to extraction. Already-selected filters use `wait_for_verified_listing` without a
click or requiring a revision change. Only extract offers after these calls succeed.

The **browser readiness observer is still missing**. Its revision must correspond to
a completed update, not simply a URL change, request start or changed card count.
It must cover delayed/debounced updates, same-card results, full navigation, SPA
refreshes and empty results. Capture actual loading/completion DOM evidence before
implementing it. Navigation completion alone must not release stale SPA results;
unrelated analytics requests must not determine readiness. The current offline tests
exercise this contract with simulated states, not actual Rainbow refresh detection.

## Locator evidence from the local recording

These are relatively stable semantic/attribute candidates observed in Codegen,
not live-verified unique locators:

| Control | Recorded locator / qualification |
| --- | --- |
| Departure panel | `[data-test-id="r-input-button:filtyGorne:skad"]` |
| Airports | `get_by_role("checkbox", name=<exact airport label>, exact=True)`; scope to the departure panel if duplicated |
| All inclusive | `[data-test-id="r-accordion:filtryBoczne:Wyżywienie"]` with a named checkbox |
| Other meals | Named checkboxes `3 posiłki` and `2 posiłki`; verify section scope and uniqueness |
| Maximum price | `get_by_role("spinbutton", name="Cena do", exact=True)` |
| Customer rating | `get_by_role("radio", name="Od 5.0", exact=True)` |

The recorded duration name `- 9 dni` is incomplete: obtain its accessible name and
section HTML before treating it as stable. Avoid `.first`, `.nth(5)`, dynamic classes
and broad `div` text matches from the recording. Sorting, result cards, loading,
completed empty results and offer-link locators still lack evidence.

Recorded URL values include `wybraneSkad` (repeated), `wyzywienia` (repeated),
`dlugoscPobytu=7-9`, `cena.do=1500`, `ocenaKlientow=10-12` and `sortowanie=cena-asc`.
The rating parameter is observed alongside the `Od 5.0` action; do not generalize
it into an arithmetic rating conversion. Keep the visible native rating unchanged.
Do not replay recorded `goto` URLs as substitutes for waiting after interactions.

## Remaining work before a full POC

Obtain DOM evidence for readiness, sorting, duration, cards, empty results and offer
links; verify locator uniqueness and price-input commit behavior (including blur).
Connect the settings and coordinator to a browser adapter and verify every selected
control as well as the cumulative URL. Confirm hotel-standard semantics separately.
Add listing parsing, bounded collection, deduplication, full variant URLs and offline
fixtures for the actual DOM. Verify per-person price, party, duration and native
rating semantics. A later explicitly authorized live check is still required;
none was run for this change.

## Manual evidence to record

From the repository root, run Playwright Codegen against `https://r.pl/`, using the
Python target, Chromium, Polish locale and a 1440x1000 viewport. Save generated code
under ignored `data/rainbow-playwright/` with a unique timestamp in the filename.
Codegen records actions, not a complete HTML/screenshot/request diagnostic bundle;
scrolling and visible card contents may require separate notes or saved DOM evidence.

Record cookies, two adults / zero children / one room, unrestricted dates if supported,
and the exact confirmed selections above. Verify default ascending price without
clicking sorting when UI and URL already agree. Investigate hotel stars separately;
do not add an assumed URL mapping.
Use the actual Rainbow controls; record any unavailable option or different duration,
price unit, rating scale or meal grouping rather than assuming ITAKA semantics.

Then record the listing, a small scroll if necessary and one example offer click.
Observe whether navigation replaces the current page or opens a popup. Preserve the
full variant URL. Stop at any CAPTCHA or anti-bot challenge; do not bypass it.
Do not perform a booking. Close the browser after recording and provide the saved
script, final listing URL and any relevant missing-control notes.

For unstable generated selectors, obtain outerHTML of the relevant section, including
its heading, labels, inputs and stable attributes. Do not turn dynamic classes,
positional selectors or forced clicks into implementation assumptions.

## Architecture for a later implementation

- Normalize listing evidence into the shared `Offer` model; keep unavailable data
  explicit and preserve the provider's native rating scale. The current model does
  not have a departure-time field: retain this in diagnostic card evidence initially.
- Read business thresholds from root configuration. Reuse `matches_criteria` for
  preliminary qualification; production `matches` still requires complete prices.
  Do not duplicate meal, country-star or rating rules, and do not claim listing prices
  are verified booking totals.
- Proposed POC configuration: `max_offers=10`, `max_analyzed_cards=15`, with independent
  bounded scroll/no-progress controls after actual lazy-loading behavior is observed.
  These are design defaults, not a runnable Rainbow configuration yet.
- Prefer UI filtering and ascending price before extraction. Read cards once per
  full variant identity; no extra request per card and no routine detail visits.
- A confirmed no-matching-results message must stop collection before alternative
  cards are considered. URL parameters alone do not prove the visible offers match.
- Keep browser access separate from parsing and collection policy. Add offline tests
  for labels/hidden controls, duplicate IDs across sections, no-results alternatives,
  budgets, deduplication, lazy loading and popup variant preservation.
- No scheduler registration, weather, external ratings or notification transport work.

See [the proposed diagnostic retention policy](../artifact_retention.md).
