# Wakacje.pl — current state (handoff, 2026-09-22)

**Read this file first.** It is the authoritative, up-to-date source of truth for
Wakacje.pl. `RECONNAISSANCE.md` in this same directory is the detailed evidence
log behind every claim here (§1-23) — consult it for exact URLs, HTTP statuses,
and raw response fields, but treat *this* file as correct if the two ever
disagree (this one is newer). `AGENTS.md` (repo root) has the workflow rules.
The project-wide handoff at `experiments/rainbow_playwright/CURRENT_STATE.md`
still covers ITAKA/Rainbow/TUI/notifications/roadmap — not duplicated here.

**One-line status:** Wakacje.pl is implemented, tested, and **fully live-verified
end to end for all four target airports together — LCJ, WAW, KTW, WRO**
(RECONNAISSANCE.md §23: 14/14 requests, 81.01s, comfortably under
`cycle_seconds=120`). The project owner has made the explicit decision to turn
it on: **`enabled: true` in `config.json`**, `interval_seconds: 3600` (a full
scan at most once an hour to start; deliberately conservative, revisit once
observed live behavior supports a shorter interval). The provider is
technically ready. **The scheduler has not been run yet** (no `--watch`, no
24/7 run) — see §5 for the next step and one known test-suite side effect of
this flip that still needs a follow-up.

---

## 1. Current implementation state

- **Base scope:** `/wczasy/` (the site's own general search — its search box
  literally reads "Dowolny kierunek lub hotel"), not `/lastminute/`. Confirmed
  materially broader (38,216 vs 23,577 matches at measurement time).
  `travel_deal_agent/providers/wakacje.py`, `LISTING_PATH`.
- **Pagination:** implemented per confirmed airport, using the site's own
  confirmed shape — page 1 is the bare `?<slug>`, pages 2+ are
  `?str-<n>,<slug>` — bounded by `cfg.get("max_pages", 2)`, with a graceful
  budget-exhaustion break (mirrors `itaka.py`). `config.json` sets
  `max_pages: 3`. `wakacje.py: fetch()`.
- **`price_is_complete` policy:** `price_is_complete` itself is **untouched**
  — still unconditionally `False` for every Wakacje.pl offer
  (`wakacje_data.normalize_offer`), for the same structural reason as always
  (no robots-compliant path to a confirmed booking total exists for this
  source). What changed: `filtering.matches()` now accepts an incomplete
  price when the offer's provider is listed in the new
  `filters["accept_incomplete_price_from"]` (a `FilterConfig` field).
  `config.json` sets `["wakacje.pl"]` — ITAKA/Rainbow/TUI/mock are not listed,
  so their behavior is bit-identical to before this change (pinned by
  regression tests). `storage.Store.observe()`'s alert gate was simplified
  from `eligible and offer.price_is_complete` to `eligible` alone — `matches()`
  is now the single source of truth; see that method's docstring for the
  exact trust contract, and `tests/test_pipeline.py` for the proof that
  `filter_batch` never passes `eligible=True` except when `matches()` agrees.
- **Notification disclaimer:** `notification_content.NotificationMessage.render()`
  no longer raises for an incomplete price. It appends the line `"Price is
  from the listing — not yet confirmed at checkout/booking."` right after the
  total-price line, only when `price_is_complete` is `False`.
- **Confirmed airport slugs (`wakacje.py: CONFIRMED_AIRPORT_SLUGS`):**
  ```python
  {
      "LCJ": "z-lodzi",
      "WAW": "z-warszawy",
      "KTW": "z-katowic",
      "WRO": "z-wroclawia",
  }
  ```
  All four of the business's target airports (LCJ/WAW/KTW/WRO) are now
  standalone live-confirmed. See §2 for exactly what "confirmed" means and
  what is still missing (only WMI, which is not one of the four target
  airports).
- **`COUNTRIES` (`wakacje_data.py`):**
  ```python
  {
      "turcja": "TR",
      "egipt": "EG",
      "tunezja": "TN",
      "grecja": "GR",
      "albania": "AL",
      "bulgaria": "BG",
      "hiszpania": "ES",
      "cypr": "CY",
  }
  ```
  `cypr` is new this cycle — direct live evidence from the final
  full-provider scan (RECONNAISSANCE.md §23): 2 real records carried this raw
  `place.country.slug`, previously unmapped (`country=None`). No entry in
  `filters.country_min_stars` for `CY`, so it uses the plain `min_stars=3`
  default like any other unlisted country — no new business rule added.
  **Malta was deliberately NOT added** — seen only as a display name on
  listing cards, never as a URL/slug (no raw evidence to map from).
- **`config.json` (`providers["wakacje.pl"]`):**
  ```json
  {"enabled": true, "interval_seconds": 3600, "max_pages": 3,
   "max_requests": 14, "timeout_seconds": 15, "cycle_seconds": 120,
   "request_gap_seconds": 5}
  ```
  `max_requests: 14` = robots(1) + baseline(1) + LCJ×3 + WAW×3 + KTW×3 +
  WRO×3 (raised from `8` now that all four airports are confirmed and
  queried; `max_pages` unchanged at `3`).
  `cycle_seconds: 120` (raised from `60` — a live run of the real
  `WakacjeProvider.fetch()` proved `60` is deterministically too small for a
  full 14-request scan at `request_gap_seconds=5`: 13 mandatory gaps alone
  cost `65s`, before any real HTTP time. See RECONNAISSANCE.md §21-§22 for
  the exact failure and the fix; `tests/test_wakacje.py:
  test_full_confirmed_scan_fits_within_the_configured_cycle_seconds` guards
  this specific regression.
  `filters["accept_incomplete_price_from"] = ["wakacje.pl"]`.
- **`enabled: true`** (operational session F, same day) — an explicit,
  separate decision by the project owner, made only after the full live
  validation in §23. `interval_seconds` stays `3600`: a full scan is at most
  once an hour to start, deliberately conservative (one full scan already
  costs up to 14 requests over ~81s at `request_gap_seconds=5`); shortening
  it is a later, separately-evaluated decision, not made here. **The
  scheduler has still never been run** — no `--watch`, no 24/7 run, no live
  Telegram message, no production SQLite write has happened for this
  provider yet. See §5 for the next step.
- **Quality gates, last run this cycle:** `pytest -q` → **1031 passed, 6
  failed**; `ruff check .` → clean; `ruff format --check .` → clean; `mypy` →
  clean (70 source files). **The 6 failures are a direct, expected
  consequence of flipping `enabled: true` on the real `config.json` that the
  shared `settings` test fixture loads, not a functional regression:**
  `Scheduler.__init__`'s own deliberate safety check
  (`travel_deal_agent/scheduler.py`) now correctly rejects any test that
  builds a `Scheduler` from the real `settings` fixture with an incomplete
  provider list (several tests in `tests/test_quality_contracts.py` and
  `tests/test_storage_scheduler.py` only wire up `[MockProvider()]`, which
  was fine while `wakacje.pl` was disabled). One more test,
  `tests/test_wakacje.py::test_registry_builds_a_disabled_wakacje_provider_without_network_access`,
  directly asserts the old "disabled by default" invariant against the real
  settings and is now simply outdated. **None of this was fixed in the
  operational-enable session** (out of its explicitly authorized scope,
  "WYŁĄCZNIE" the config change) — it is next-session work, see §5.

## 2. Confirmed airports — exact evidence level for each

| Airport | Slug | Standalone live-verified? | Evidence |
|---|---|---|---|
| **WRO** | `z-wroclawia` | ✅ Yes | `GET /wczasy/?z-wroclawia` → `200`, every offer `departurePlaceCode == "WRO"` (RECONNAISSANCE.md §12.1) |
| **WAW** | `z-warszawy` | ✅ Yes | `GET /wczasy/?z-warszawy` → `200`, 10/10 offers `departurePlaceCode == "WAW"`, zero WMI/RDO/other (RECONNAISSANCE.md §18) |
| **LCJ** | `z-lodzi` | ✅ Yes | `GET /wczasy/?z-lodzi` → `200`, 10/10 offers `departurePlaceCode == "LCJ"`, no other code in the sample (RECONNAISSANCE.md §21). Previously only seen via manual UI as part of a multi-filter URL (§16, §20) — this is its first standalone confirmation. |
| **KTW** | `z-katowic` | ✅ Yes | `GET /wczasy/?z-katowic` → `200`, 10/10 offers `departurePlaceCode == "KTW"`, no other code in the sample (RECONNAISSANCE.md §21). Previously only seen inside robots-disallowed comma-joined per-offer hrefs (§13d) and a multi-filter UI URL (§16, §20) — this is its first standalone confirmation. |
| WMI | — | Not added | No slug evidence found anywhere, in any session. |
| RDO | — | Not added | Real, distinct code (Warszawa-Radom) seen in raw data, but not one of our configured airports (`filters.airports` has WAW/WMI, not RDO). |
| — | `z-warszawa-chopin` | ⚠️ **Tested standalone, confirmed to NOT work** | `GET /wczasy/?z-warszawa-chopin` → `301`, `Location: /wczasy/` — the site silently *drops* this exact filter shape rather than honoring or canonicalizing it (RECONNAISSANCE.md §17). **Never use this slug for anything.** This is the concrete proof that "confirmed to exist in the UI" ≠ "works as a standalone single-flag filter" — the reason LCJ/KTW must be live-tested, not just UI-confirmed, before being added. |

## 3. Real offers found manually — business regression examples

These are not synthetic. Recorded by hand from the live site during a manual
comparison session (RECONNAISSANCE.md §16). Full raw values are in
`tests/test_wakacje_data.py`'s `ALION_OFFER` fixture and its two tests
(`test_real_alion_scenario_normalizes_as_observed`,
`test_real_alion_scenario_passes_matches_end_to_end`).

| Hotel | Country | Airport | Price/person | Board | Stars | Rating | Outcome |
|---|---|---|---|---|---|---|---|
| HTop Olympic (Calella) | Hiszpania | WRO | 1489.50 PLN | HB | 4★ | 7.2 | **Correctly rejected** — rating 7.2 < 8.0 required for the 1000-1500 PLN band (`filters["provider_ratings"]["wakacje.pl"]`). Would *also* have been rejected pre-fix on unmapped country (now moot — Hiszpania is mapped). |
| Alion (variant 1: 03.10-10.10.2026) | Albania | KTW | 1497 PLN | HB | 4★ | 8.6 (12 opinii) | Seen on the results list, but the specific detail page later showed **"Oferta w tej konfiguracji jest niedostępna"** — real-time inventory turnover, not a bug in our code. KTW is now a confirmed airport (§2), but this specific dated variant may no longer be available regardless. |
| **Alion (variant 2: 30.10-06.11.2026)** | Albania | **WAW** | **1479 PLN** | HB | 4★ | 8.6 (12 opinii) | **`matches() == True` after this cycle's changes** (proven by `tests/test_wakacje_data.py::test_real_alion_scenario_passes_matches_end_to_end`). Before this cycle it failed on two independent grounds: `offer.country is None` (Albania unmapped) and WAW never deliberately queried. Both fixed. |

**Also observed, not clicked into (no exact URL/full data captured):** the
business-filtered search (≤1500 PLN/os., AI/HB/FB, 3★+, rating ≥8.0, departing
Katowice/Łódź/Warszawa/Wrocław) returned exactly 6 real offers at search
time — Meridian (**Bulgaria**) ×2 date variants and Pebbles Resort
(**Malta**) ×1, alongside the 3 Alion variants above. **Zero were from
Turcja/Egipt/Tunezja/Grecja** — this was the single most load-bearing finding
that drove the `COUNTRIES` expansion. Malta was *not* added (no slug
evidence); Bulgaria *was* added and is now covered.

## 4. Business requirements audit (COVERED / PARTIAL / NOT COVERED)

| # | Requirement | Status | File / function |
|---|---|---|---|
| 1 | 2 adults | COVERED | `wakacje_data.ASSUMED_PARTY_SIZE`; `filtering.matches_criteria` |
| 2 | ≤1500 PLN/person | COVERED | `filtering.matches_criteria` |
| 3 | LCJ/WAW/KTW/WRO | COVERED | Business config (`filters.airports`) has all 4; provider-level `CONFIRMED_AIRPORT_SLUGS` now also has all 4 (§2) |
| 4 | Łódź ranking priority | COVERED, now active | `config.json: ranking.airport_groups` (LCJ its own top group); `ranking.score()`. LCJ is now deliberately fetched, so this bonus is live, not just mechanically present. |
| 5 | min 3★ | COVERED | `filters.min_stars`; `filtering.matches_criteria` |
| 6 | African destinations min 4★ | COVERED for mapped countries (EG, TN today) | `filters.country_min_stars` (generic, config-driven, no hardcoding — verified by code search) |
| 7 | customer rating | COVERED | `ratings.rating_matches`; `filters.provider_ratings["wakacje.pl"]` |
| 8 | review count | **NOT COVERED** (deliberate) | `wakacje_data.normalize_offer`: `number_of_reviews=None` always — `ratingReservationCount` semantics never confirmed, never mapped. Not a hard filter for any provider, but zeroes the "reviews" ranking component for every Wakacje.pl offer. |
| 9 | board/meal type | COVERED | `wakacje_data.SERVICE_BOARDS`; `filters.allowed_boards`; `boards.board_matches_price` |
| 10 | stay length | COVERED | `filters.min_days`/`max_days`; `filtering.matches_criteria` |
| 11 | dates | COVERED | `matches_criteria` (departure ≥ today); date/duration consistency validated at parse time |
| 12 | country | **PARTIAL** | Mechanism is correct and fail-closed; `COUNTRIES` covers only 8 slugs today — everything else is deliberately unrecognized, not a bug |
| 13 | region | COVERED (descriptive only, not filtered) | `destination` field |
| 14 | total price and price/person | COVERED | `Decimal`, exact halves, `wakacje_data.normalize_offer` |
| 15 | `price_is_complete` / listing-price policy | COVERED | §1 above |
| 16 | variant deduplication | COVERED | `wakacje_data.variant_identity`; `models.duplicate_key`; `ranking.deduplicate` |
| 17 | same hotel, different dates | COVERED | Same `variant_identity` mechanism (departure date is part of the identity) |
| 18 | pagination | COVERED for all 4 confirmed airports | `wakacje.py: fetch()` |
| 19 | `max_requests` / rate limiting | COVERED | `RequestBudget`, `robots_policy`, `request_gap_seconds`; `cycle_seconds` tuned this session so a full 14-request scan can actually complete (§1, RECONNAISSANCE.md §22) |
| 20 | no duplicate alerts | COVERED | `alerts.classify_alert`; `storage.alert_state` (keyed by `duplicate_key`) |
| 21 | price history | COVERED | `storage.price_history`; `Store._save_price` |
| 22 | ranking + Łódź bonus | COVERED, now active | same as #4 |

## 5. Next step for the new session — start here

All four target airports are standalone live-confirmed (RECONNAISSANCE.md
§21) and implemented (§1-§4). The `cycle_seconds` blocker found by the first
full-provider live attempt is fixed and re-confirmed live (RECONNAISSANCE.md
§22-§23): the final controlled live test completed **14/14 requests in
81.01s**, comfortably under `cycle_seconds=120`, with all four confirmed
airports queried together for the first time and each returning exclusively
its own `departurePlaceCode`. That same scan surfaced one new, genuine raw
country (`"cypr"`), now mapped (§1, RECONNAISSANCE.md §23). The project owner
has since made the explicit decision to enable it: **`enabled: true`**,
`interval_seconds: 3600` (§1).

**Two things before any real scheduler run:**

1. **Fix the 6 test failures caused by the `enabled: true` flip** (§1 has the
   exact list and root cause — `Scheduler`'s own safety check correctly
   rejecting test setups that assumed `wakacje.pl` would stay disabled in the
   real config the shared `settings` fixture loads). This is ordinary
   test-suite upkeep, not a functional bug in the provider or the scheduler;
   it was left undone deliberately because the enabling session was scoped
   to the config change only.
2. **Run one normal, controlled run of the application with Wakacje.pl as an
   active provider**, with DB writes, the notification outbox and Telegram
   delivery all under deliberate control (matching how ITAKA/Rainbow/TUI were
   each first brought live) — not `--watch`, not unattended 24/7, until that
   controlled run's results are reviewed. This is the next real milestone;
   nothing before it authorizes an unattended scheduler run.

If a *future* live check ever surfaces something unexpected (redirect, mixed
codes, error, CAPTCHA, a `departurePlaceCode` that doesn't match the
requested airport, a new unmapped country, etc.) for any of the four
airports: stop, report exactly what happened (mirroring the
`z-warszawa-chopin` write-up in RECONNAISSANCE.md §17).

## 6. Hard constraints (do not relax these without an explicit, separate decision)

- **Never guess a slug.** Every entry in `CONFIRMED_AIRPORT_SLUGS` must have a
  live, standalone `200` response with matching `departurePlaceCode` — a slug
  merely existing in the UI or in a multi-filter URL is not sufficient
  (`z-warszawa-chopin` is the proof why: real, UI-generated, and still a
  `301`).
- **Respect `robots.txt`** via `robots_policy` for every single request,
  every session, re-fetched fresh each time (it has been byte-identical
  across every session so far, but never assume that holds).
- **No mass crawling.** Every live session in this project's history has used
  the minimum number of requests needed to answer a specific question,
  announced individually.
- **No `?tanio` in the MVP.** Confirmed dead end for our airport-based
  criteria (§13c of RECONNAISSANCE.md) — cheapest-sorted results are
  dominated by no-flight offers that would fail `RawOffer` validation anyway.
- **`price_is_complete` stays `False` for Wakacje.pl.** This is structural
  (no robots-compliant path to a confirmed booking total exists for this
  source), not a temporary gap. Do not attempt to make it `True`.
- **A listing price may only ever alert through the provider whitelist**
  (`filters["accept_incomplete_price_from"]`), never by weakening
  `filtering.matches()`'s check globally, and never by re-adding a
  price-completeness check to `storage.py` that duplicates that whitelist.
- **An unrecognized country still fails closed** (`offer.country is None` →
  rejected). Extending `COUNTRIES` is always additive and evidence-gated,
  never a default-permissive fallback.
- **LCJ has the highest business/ranking priority** of all four target
  airports (`ranking.airport_groups`'s first, standalone group) — its coverage
  gap is now closed (§1-§4), and its ranking bonus is live.

## 7. Roadmap after Wakacje.pl

Per the project owner's stated priorities, in order:
1. All four target airports (LCJ/WAW/KTW/WRO) are now confirmed and
   implemented (§1-§4). What remains: the one controlled live test of all
   four together (§5), then a deliberate, separately-authorized decision on
   `enabled: true`.
2. TUI — resume its own follow-ups (see
   `experiments/rainbow_playwright/CURRENT_STATE.md` §2 for what's already
   known/open there).
3. Continue the project's other next stages (see the project-wide
   `experiments/rainbow_playwright/CURRENT_STATE.md` for the fuller
   ITAKA/Rainbow/TUI/notification roadmap — not duplicated here).
4. A dedicated, separate review of the whole codebase against
   SOLID/KISS/DRY and readability/testability/maintainability — explicitly
   **without overengineering**. Not started yet; treat as its own scoped
   piece of work, not something to fold into provider-specific sessions.
