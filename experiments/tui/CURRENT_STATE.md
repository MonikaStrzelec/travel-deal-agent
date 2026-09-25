# TUI provider — current state (2026-09-23)

This is the canonical TUI handoff. (An earlier session's TUI notes live inside
`experiments/rainbow_playwright/CURRENT_STATE.md` for historical reasons; this
file is the up-to-date one going forward.)

## 1. Provider status

Full, offline-tested provider (`travel_deal_agent/providers/tui.py`,
`tui_data.py`, `tui_query.py`, `tui_browser.py`, `tui_price.py`,
`tui_errors.py`). **`config.json` → `providers.tui.enabled: false`.** Not
enabled by this or any prior session. `registry.py` wires it the same
disabled-unless-configured way as ITAKA/Rainbow.

Real flow: HTTP `robots.txt` (fail-closed, union of all `Disallow`) → one
passive Playwright navigation per listing page (page 1, then up to
`max_pages - 1` more; see §3) to the confirmed `/wypoczynek/wyniki-
wyszukiwania-samolot?q=...` search URL, capturing the page's own
`search/offers` XHR response → normalize → dedup by `offer_id` → at most
`max_detail_requests` (config: `1`, hard-capped at `3`) additional passive
detail-page navigations to confirm real-time price/availability.

**1129 tests pass** (`pytest -q`); `ruff check`, `ruff format --check` and
`mypy` are all clean.

## 2. Price confirmation

`tui_price.py::confirm_realtime_price` passively captures
`.../api/services/tui-search/api/search/offers/price?offerCode=...
&mode=REALTIME`, triggered by TUI's own detail page (never fetched directly).

**`price_is_complete=True` only when ALL of these hold:**
- exactly one matching REALTIME response (ambiguity/timeout → unconfirmed, no crash),
- `offerCode` matches the listed offer,
- `offerStatus == "AVAILABLE"`,
- currency `PLN` (`priceDifference` is informational only, not a gate --
  confirmed 2026-09-25 by a real `AVAILABLE` response reporting
  `priceDifference=-2` that exactly equalled `totalPrice - listing total`
  ; the realtime `totalPrice`/`pricePerPerson` are already the authoritative,
  current price regardless of this delta, so a nonzero value alone never
  blocks confirmation),
- `travellerCount == {adults: 2, children: 0}`,
- hotel code / departure airport / dates / duration all agree with the listing,
- no unrecognized field inside `priceDetails` (possible new fee → reject),
- **the offer is structurally confirmed as a charter-flight package tour**:
  `"CHARTER_FLIGHT" in tags` **and** `analyticsData.values.flight_type ==
  "CHART"` **and** `offerTravelType == "BYPLANE"` (three independent fields,
  not inferred from price or airline name),
- `priceDetails.priceGuaranteeFund` exactly equals the officially confirmed
  rate `(TFG 15 + TFP 15) PLN/traveller × adults` (2026-09-2x, project owner,
  from TUI's own published terms + the TFG regulation). Any other value →
  rejected (possible rate change), not silently trusted.

When confirmed: `booking_total_price = priceDetails.totalPrice +
priceGuaranteeFund`, `price_per_person = booking_total_price / 2`,
`operator_mandatory_fees = [TFG, TFP]` (split, each `15 × adults`).

**Confirmed only for charter air packages.** A non-charter (e.g. scheduled/
"liniowy") TUI offer, or one that can't be classified either way, is left with
`price_is_complete=False` and reason `CHARTER_PACKAGE_NOT_CONFIRMED` — its own
mandatory-fee rate is not known and is not guessed.

Real evidence: `data/tui-production/realtime-recon-20260922T191702Z/`
(Sun City Apartments & Hotel, KTW → Antalya, 2 adults: 2786 + 60 = 2846 total,
1423/person, `tags=["CHARTER_FLIGHT", "FIRST_MINUTE"]`,
`analyticsData.values.flight_type="CHART"`).

## 3. Pagination — CONFIRMED LIVE and implemented

**Confirmed live, 2026-09-22**
(`data/tui-production/pagination-recon-20260922T200838Z/`): a deliberately
broader query (all confirmed non-disabled airports, `amountRange` raised to
10000, the full confirmed `dF:6-dT:14` duration envelope — no new/guessed
parameters) returned `pagination: {"page": 0, "pageSize": 20, "totalResults":
3469, "pagesCount": 174}`. One additional navigation with
`build_search_path(filters, page=2)` returned `pagination.page == 1` and a
**completely disjoint set of 20 `offerCode`s** from page 1 — genuinely new
results, not a repeat.

**Implementation** (`tui.py::TuiProvider._fetch_additional_pages`):
- Page 1 is always fetched; its `pagination.pagesCount` (an absent/malformed
  value is treated as `1`, never guessed higher) determines how many more
  pages exist.
- Fetches `min(pagesCount, max_pages) - 1` additional pages (2..N), each via
  the same passive `search/offers` capture as page 1 — no direct `/api/`
  request, ever.
- `max_pages` (config: `3`, hard-capped at `3` by `config.py`, `null`/
  unlimited rejected at startup) bounds this regardless of how large a real
  `pagesCount` is (confirmed live: 174 pages exist for a broad query; this
  provider will still only ever fetch at most 3).
- A `TuiTimeout` on page 2 or 3 keeps every page already fetched and simply
  stops further pagination for that cycle. Any other failure (`TuiBlocked`,
  `TuiStructureError`, malformed JSON, a robots-disallowed path) still fails
  the whole cycle, exactly as before pagination existed.
- Offers are deduplicated by `offer_id` across all fetched pages before detail
  confirmation runs, so an accidental repeat between pages is never
  double-counted; distinct dates/variants of the same hotel have distinct
  `offer_id`s and are never collapsed.

Tests: `tests/test_tui_pagination.py` (pagesCount=1/3/10 × max_pages=1/3,
new-offers-per-page, dedup, different-dated variants, timeout-keeps-earlier-
pages, block/ambiguous/malformed-still-fails, missing-pagination-defaults-to-
one-page, combined browser-navigation-budget, disabled rating not blocking).

## 4. Rating — MVP decision (no longer an open question)

**MVP decision: no TUI hard rating filter; use rating/review data in
ranking.** No previously agreed TripAdvisor threshold for TUI exists anywhere
in this repo, and none was invented. `provider_ratings.tui` stays disabled
(`config.py::load_settings()` defaults any registered provider without its own
entry to `{"enabled": false, "scale": null, "price_bands": []}`) — this is a
deliberate, permanent-for-MVP choice, not a placeholder pending a number.
TUI's TripAdvisor rating (1–5) and review count still flow unconditionally
into `Offer.rating` / `Offer.number_of_reviews` and into the shared ranking
score, exactly like every other provider's native rating — only the *hard
filter* is off. Confirmed live (§6 smoke): every fetched offer carried a real
`rating`/`number_of_reviews` pair.

## 5. Local mandatory costs — known limitation (unchanged)

Malta's hotel tourist tax (~1.50 EUR/day/person, capped ~22.50 EUR/person,
paid at reception) was only ever observed in the rendered page's free-form
body text, never in any structured JSON field (`offerData`, `price/REALTIME`,
`alternatives`). `models.LocalMandatoryCost` / the generic mechanism ITAKA uses
(`itaka_details.local_costs()`, which scans *structured* practical-information
objects) has no structured TUI source to read yet. Not implemented — a
text/HTML scraper would be fragile and is deliberately avoided.
`Offer.local_mandatory_costs` stays `[]` for TUI, which the shared
`notification_content.py` already renders as "no data (does not mean no
costs)" — never "zero cost". `operator_mandatory_fees` (TFG/TFP) and
`local_mandatory_costs` (this) are already distinct fields, rendered
separately; no shared-model change was needed or made.

## 6. Browser-navigation budget + live smoke result

TUI is heavier than Wakacje.pl: every request is a full Playwright page load,
not a plain HTTP GET. Per cycle: at most `max_pages` listing navigations
(config: `3`, hard-capped at `3`) + at most `max_detail_requests` detail
navigations (config: `1`, hard-capped at `3`) — at most **4** navigations for
the shipped config, enforced at startup validation (`config.py`), not a
runtime clamp.

**Controlled provider-only live smoke, 2026-09-22** (real business filters
from `config.json`, `TuiProvider` constructed in-memory with `enabled: true`
only for this one call — `config.json` itself untouched, still `false`; no
`Store`, no notifier, no `Scheduler`, no `--watch`):
- Real query still returned `pagesCount == 1` for these narrower business
  filters (LCJ/WAW/WMI/KTW/WRO, ≤1500 PLN/person, 7–9 days) — pagination logic
  ran but correctly fetched only page 1, exactly as it should when there is
  only one page.
- **10 offers** after normalization/dedup (1 raw record rejected by the
  existing per-record duration consistency check — a normal, isolated
  rejection, not a cycle failure).
- 1 detail-confirmation attempt (the cheapest candidate): real-time check
  returned `offerStatus=NOT_AVAILABLE` this time (the offer had gone stale) —
  **0 offers had `price_is_complete=True`** this run. This is a legitimate,
  time-dependent business outcome, not a defect (ITAKA's own live runs have
  shown the same pattern).
- **0 offers passed `filtering.matches()`** — dominated entirely by
  `price_is_complete=False` on every offer this run (no other filter was the
  blocker: airports, price range and stay length all otherwise looked
  plausible).
- price_per_person range observed: **1104–1516 PLN**; departure airports seen:
  **KTW, WAW, WRO** (LCJ/WMI not present in this particular result set); every
  offer carried a real `rating` (2.9–4.0) and `number_of_reviews` (105–3302).
- No unexpected errors; the only warning was the single expected per-record
  rejection above. Total navigations this run: 2 (1 listing page + 1 detail
  attempt), well inside the 4-navigation budget.
- No second smoke was run automatically, per instructions.

## 6a. Aggregate cycle deadline (new, 2026-09-23)

Per-navigation `timeout_seconds` already bounded each individual Playwright
navigation, but nothing bounded the *whole* cycle's wall-clock time. With
`max_pages=3` and `max_detail_requests` up to 3, a slow-but-individually-
successful sequence of navigations could still run long and delay the other
(sequential) providers behind TUI in the scheduler.

`providers.tui.cycle_seconds` (config, **`90`**) now bounds the whole cycle.
Chosen conservatively from the shipped config's own numbers: worst case is
`max_pages (3) + max_detail_requests (1) = 4` navigations at `timeout_seconds
(20)` each = 80s; `90` covers that worst case with a small buffer without
being absurdly high (compare ITAKA `60`, Wakacje.pl `120`, Rainbow `180`). A
normal live cycle (§6 smoke: **2** navigations, each well under its 20s
timeout) finishes in a small fraction of this budget.

**Mechanism** (`TuiProvider`, no new Budget framework): `deadline =
injected_clock() + cycle_seconds` is computed once at the top of `fetch()`
(mirroring `RequestBudget`/Rainbow's `BrowserListing.deadline`), using a new
`clock: Callable[[], float] = time.monotonic` constructor parameter (same
injection pattern as `ItakaProvider.clock` / `RequestBudget.clock` — no
bare `time.monotonic()` call sits untestable in the flow). The deadline is
checked before every Playwright navigation *after* the first: page 1 is
always fetched unconditionally (mirrors ITAKA/Rainbow's own always-fetched
first request); pages 2..N and each detail navigation are gated on it.

**Behavior on expiry — deliberately different from `RequestBudget`'s hard
`ValueError`:** exceeding the cycle deadline is treated exactly like an
already-existing transient `TuiTimeout` on page 2/3, not like ITAKA/Wakacje's
`RequestBudget.get()` raising and failing the whole cycle:
- **Pagination:** the next page's navigation is skipped, every page already
  fetched is kept, and `fetch()` returns those offers normally (no exception).
- **Detail confirmation:** the next candidate's detail navigation is skipped;
  it (and every remaining candidate) stays with `price_is_complete=False`
  (whatever it already had) — never faked as confirmed. Once the deadline
  trips, no further per-candidate `clock()` calls or navigations happen.
- `TuiBlocked`, `TuiStructureError`, robots failures and a single navigation's
  own `TuiTimeout` are untouched by this and still propagate/fail the whole
  cycle exactly as before — the cycle deadline never weakens fail-closed
  behavior, it only stops **starting new work** once the budget is spent.

**Tests** (`tests/test_tui_pagination.py`): deadline-after-page-1 stops page
2 (page 1 kept), deadline-after-page-2 stops page 3 (pages 1+2 kept),
deadline-before-detail leaves the offer incomplete with no detail navigation
attempted, and a full 4-navigation cycle completing comfortably inside
`cycle_seconds`. `tests/test_quality_contracts.py` adds config validation:
`cycle_seconds <= 0` rejected (generic "HTTP limits must be positive" check
in `config.py::validate_options`, already shared with ITAKA/Wakacje — no new
TUI-specific check was added), wrong type rejected (pydantic `strict=True`),
and an unknown field under `providers.tui` rejected (existing closed-TypedDict
behavior). All prior pagination/timeout/block/structural-error tests are
unchanged and still pass, confirming those paths are untouched.

## 7. Hard rules (robots / Playwright) — unchanged, still binding

- `robots.txt` fetched fresh every cycle; conservative union of all
  `Disallow` (any user agent), fail-closed on missing/malformed robots.
- Every Playwright navigation goes to an already robots-checked
  `/wypoczynek/...` URL — never `/api/...` directly, never a configurator,
  checkout or reservation path.
- Passive listening only: no clicks, no scrolling to trigger data, no form
  fills, no login.
- `tests/conftest.py`'s autouse `no_network` fixture blocks real sockets and
  `sync_playwright` in both `tui_browser.py` and `rainbow_browser.py` during
  the whole test suite.

## 8. Known limitations (summary)

- Local mandatory costs not extracted for TUI (§5) — no structured source found.
- TFG/TFP rate confirmed only for charter-flight packages; a non-charter TUI
  offer cannot yet reach `price_is_complete=True` (no confirmed rate for it).
- `offerCode` stability across repricing/days is still unconfirmed (only
  same-session stability has been observed).
- No agreed TripAdvisor hard-filter threshold — **by MVP decision, not a gap**
  (§4); rating/reviews still reach ranking.
- The live smoke (§6) happened to observe zero `price_is_complete=True`
  offers (the one detail candidate had gone stale) — this is expected
  business variance, not evidence the mechanism doesn't work (it was already
  proven to work against the real Sun City example, §2).

## 9. Blocker before `enabled=true` / readiness for `--watch` and Docker

No remaining **technical** blocker was found. `providers.tui` now has both a
per-navigation timeout (`timeout_seconds`) and an aggregate cycle deadline
(`cycle_seconds`, §6a) bounding how long one TUI cycle can run and block the
sequential scheduler, matching the same shape ITAKA/Rainbow/Wakacje.pl
already have. The one thing worth a conscious decision before running TUI
continuously (`--watch`, Docker/VPS) is operational, not technical:
confirming the desired `interval_seconds`/`interval_min_seconds`/
`interval_max_seconds` cadence and that the project owner is ready for TUI to
make real, recurring Playwright requests against the live site (mirroring
the same deliberate, separate decision already made for ITAKA). Local
mandatory costs and the rating threshold (§4, §5) are explicitly accepted MVP
gaps, not blockers.
