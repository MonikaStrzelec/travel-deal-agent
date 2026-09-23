# ITAKA Playwright proof of concept

Experimental browser access; the production provider is unchanged. Eligibility rules
are shared through `matches_criteria`; production `matches` still requires complete prices.
Do not run against ITAKA without explicit authorization. This revision was tested offline
only; the complete live flow and result-refresh timing still need validation.

## Search flow

An authorized start creates a fresh browser context, waits for the observed cookie
consent button, verifies and saves two adults / zero children / one room, leaves dates
unrestricted, selects 7-9 days, selects Warszawa, Katowice, Wrocław and Łódź, and searches.
The default party is verified inside its dialog; unexpected defaults stop the run instead
of guessing unnamed increment/decrement controls. Warszawa includes WAW and WMI.

Before reading cards, the POC selects ascending price, applies the configured minimum
stars and maximum price, then checks the observed URL parameters `order`, `hotelRating`
and `priceTo` without reopening the panel. It waits for a visible result card and for
the first N cards and URL to remain unchanged for one second, bounded by a 15-second
stability timeout. This quiet period is a heuristic, not proof that all background
requests have finished. A timeout (including an empty result set without a verified
empty-state selector) is an explicit diagnostic failure, not proof of zero offers.

Configuration:

| File | Setting | Current value |
| --- | --- | --- |
| Root `config.json` | `filters.max_price` | 1500 PLN/person |
| Root `config.json` | `filters.min_stars` | 3 |
| Root `config.json` | `filters.currency` | PLN (other currencies rejected) |
| POC `settings.json` | `max_offers` | 10 |
| POC `settings.json` | `max_analyzed_cards` | 15 |
| POC `settings.json` | `max_scrolls` | 20 |
| POC `settings.json` | `max_idle_scrolls` | 3 |
| POC `settings.json` | `scroll_wait_ms` | 1500 |

Party size, unrestricted dates, 7-9 days, the four departure cities and ascending sort
are fixed scenario choices. Country-specific 4-star rules, meals, ratings/reviews,
ranking, Łódź bonus, history and external verification remain in Python. This POC
does not execute the production pipeline or claim to verify complete booking prices.

## Locator evidence

- Cookies: button `Akceptuję wszystkie`, with the existing delayed-dialog wait.
- Party: exact button `Ile osób 2 os., 1 pokój`; dialog text confirms room/adult/child counts.
- Duration: exact button `Kiedy i na ile dowolnie`, then visible text `7-9 dni` in the
  dialog; `get_by_label("7-9 dni", exact=True)` only checks state of the hidden input.
- Airports: exact button `Skąd i jak Dowolnie`, first `rozwiń` inside the dialog
  (observed airport expander; the second is for coach cities), exact checkbox city names.
- Save/search: exact buttons `Zapisz` (dialog-scoped) and `Szukaj`.
- Sort: button name beginning `Sortuj:`, exact option button `Najniższa cena`;
  selected text is checked after choosing and applying filters.
- Filters: button name ending `Filtry`, allowing its observed leading icon.
- Stars: visible `label[for="hotelRating-3"]` (threshold from configuration);
  the associated `input[name="hotelRating"][value="3"]` only verifies checked state.
- Price: `input[name="priceTo"]`, filled from `max_price`, checked before saving;
  the results URL confirms the applied value afterward.
- Apply: exact button `Pokaż oferty`.

Before applying, meals are set to exactly A (All inclusive), V (3 posiłki) and H
(2 posiłki). F, U and X are unchecked if necessary. Within
`#modal-offcanvas-container`, click the visible `label` containing the corresponding
`input[type="checkbox"][id="A"]` (and other confirmed IDs); use inputs only to
check state. No separate Ultra All Inclusive option is implemented.

No generated Radix IDs, dynamic CSS classes, `nth-child` or forced clicks are used.

## Listing data and optional details

The collector reads loaded `offer-list-item` cards in UI order and evaluates each
distinct full variant URL once. It stops immediately at 10 eligible candidates or
15 analyzed cards. If more candidates are needed, one 600-pixel wheel movement and
a bounded wait allow the site to load more cards. At most 20 movements are allowed;
three consecutive movements without new URLs stop collection as `no_new_cards`.
This is not proof that all advertised results are exhausted. Pagination is not clicked.
Only card identities are rescanned; seen variants are not normalized or filtered again.

`offers.json` now contains only preliminary candidates. `collection.json` reports
analyzed cards, scroll count, candidate count and the stop reason. Shortlist normalization
uses the existing ITAKA country/airport maps and shared meal/rating/country-star/date/
budget rules. Unknown required values fail closed. Dates, meals and ratings are parsed
only from observed card wording; a displayed TFG/TFP supplement is included in the
preliminary price. No price is marked complete and no candidate enters storage,
ranking, external verification or notification delivery. Missing review counts remain
unknown; the current production filters do not impose a review-count minimum.

Cards retain the full text and the original variant URL from
`offer-list-item-button`, plus these separate fields when available:

- `hotel_name`: level-3 heading;
- `destination`: `offer-list-item-destination` (source wording, not normalized country);
- `price_text`: `current-price` (advertised price/person, not confirmed booking total);
- `rating_text`: `reviews-rating` (preserves native /6 scale);
- `star_icons`: count of list items inside `rating-stars` (not a half-star decoder).

The saved listing also exposes review count, departure/return date text, number of days,
departure city/time, board description and displayed TFG/TFP supplement in full card text.
These remain raw evidence, not normalized or verified booking fields. Missing structured
fields remain null. Do not assume every offer has every field or every mandatory cost.

No details open automatically. After listing extraction an operator can inspect a single
candidate among the first N only when data is missing, for example:

```json
{"action":"click","role":"link","name":"Sprawdź ofertę","index":0,"missing_data":"Confirm booking total and mandatory fees"}
```

This preserves `expect_popup()` around `get_by_test_id("offer-list-item-button")` and
waits for DOMContentLoaded on the returned tab. A session allows at most one detail
opening and 30 manual checkpoints. Other diagnostic commands remain `click`, `check`,
`snapshot`, `offers`, `links`, `finish`; after a popup they act on that detail tab.
The card and scroll limits bound analysis and UI actions, not all background requests
made by ITAKA. The collector itself makes no per-card requests or detail visits.

## Setup and verification

```powershell
.\.venv\Scripts\python.exe -m pip install -r experiments/itaka_playwright/requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m pytest tests experiments/itaka_playwright -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy --explicit-package-bases travel_deal_agent tests experiments/itaka_playwright
```

Offline Chromium tests use local HTML and disabled network access. Existing duration
and popup regressions are retained; new tests cover semantic filters, configurable
budgets/thresholds, no panel reopening, delayed card updates, party/date/airport setup
and bounded card extraction.
Fixtures model supplied Codegen and saved DOM; they do not prove live site behavior.

After separate explicit authorization, launch once with:

```powershell
.\.venv\Scripts\python.exe -m experiments.itaka_playwright
```

Artifacts, screenshots and request logs are written under ignored `data/itaka-playwright/`.
There are no scheduled runs, retries, external notifications or login automation.
Stop on any challenge; do not operate booking or security controls.

## Preserved evidence

The earlier 2026-09-20 sessions recorded the delayed cookie overlay, the party/duration/
airport dialogs and the results listing. The duration regression clicks the visible
label because the input is hidden. The popup regression verifies switching tabs while
preserving the selected variant URL. This revision extends those fixes with the new
Codegen evidence; it does not replace the production HTTP integration.
