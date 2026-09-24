# Wakacje.pl — current state (handoff, last updated 2026-09-24)

**Read this file first.** It is the authoritative, up-to-date source of truth for
Wakacje.pl. `RECONNAISSANCE.md` in this same directory is the detailed evidence
log behind every claim here (§1-26) — consult it for exact URLs, HTTP statuses,
and raw response fields, but treat *this* file as correct if the two ever
disagree (this one is newer). `AGENTS.md` (repo root) has the workflow rules.
The project-wide handoff at `experiments/rainbow_playwright/CURRENT_STATE.md`
still covers ITAKA/Rainbow/TUI/notifications/roadmap — not duplicated here.

**One-line status (2026-09-24, RECONNAISSANCE.md §24-27):** Wakacje.pl now
fetches **one confirmed combined search query** in a single URL — 4 airports
(LCJ/WAW/KTW/WRO), flight only, board AI/HB/ZO/FB, min 3★, rating min 8.0,
cheapest-first sort, per-person price view — instead of the old
unfiltered-baseline-plus-per-airport flow. `do-1500zl` (the URL price cap seen
in the human-confirmed search) is deliberately **excluded**: it matches
robots.txt's `Disallow: /*?do-*` and is never sent; the business cap
(`filters.max_price = 1500` PLN/person) is enforced client-side by
`filtering.matches_criteria` exactly as before. Pagination: max 3 pages,
`max_requests: 4` per cycle (robots + 3 pages), down from 14.

**The scheduler HAS now been run** (2026-09-24, RECONNAISSANCE.md §27): one
explicitly authorized, controlled `scheduler.run_once(force=True)` cycle with
only Wakacje.pl enabled, real production DB writes and real Telegram delivery.
Result: 4/4 requests, 30 raw offers fetched, **30/30 normalized (zero parser
warnings this run)**, 8 matched `filtering.matches()`, all 8 saved to price
history and **delivered as real Telegram `new_offer` alerts**. Real examples
confirmed among the matches: Meridian at both 1389 PLN/os. (KTW) and 1393
PLN/os. (WAW), Alion at 1479 PLN/os. (WAW), Pebbles Resort at 1497 PLN/os.
(WMI) — matching what the project owner had found manually on the site.
Full detail: §5.

---

## 0. Business-rule update (2026-09-24, offline session, no live request)

Supersedes the matching statements further down (§1, §3, §4, §6):

- **Stars:** one rule for every country, `filters.min_stars = 3`.
  `filters.country_min_stars` was removed from config, types, validation,
  `filtering.matches_criteria` and Rainbow's enrichment shortlist.
- **Countries:** no whitelist. The source carries no ISO code (only
  `place.country.slug`, a Polish display name and an internal numeric id —
  checked offline in the saved `__NEXT_DATA__`), so `COUNTRIES` remains an
  evidence-only slug→ISO map for display. An unmapped country now yields
  `country=None` **and is still eligible**; its source name is prepended to
  `destination` (e.g. `"Malta / Wyspa Malta / St. Paul's Bay"`). A country that
  *is* set must still be a two-letter uppercase code.
- **Rating:** `provider_ratings["wakacje.pl"]` uses one price-independent
  `min_rating: 8` (`price_bands: []`); the 7.0 (<1000 PLN) / 8.0 band pair is
  gone. No rating filter is sent to the site: `ocena-7` exists in the sidebar
  catalog (RECONNAISSANCE.md §11.3) but was never tested standalone nor
  combined with an airport flag, so it is not used.
- **Alerts:** `price_drop_pln` (used only for alert qualification — price
  history records every change independently) was replaced by
  `alert_rearm_hours: 24`. A qualifying offer group alerts once
  (`new_offer`), then again on **any** drop below its lowest alerted price
  (`price_drop`, no minimum amount), or as `new_offer` after it went
  `alert_rearm_hours` without an eligible observation. An unchanged (or higher)
  price in later hourly scans never re-alerts. Price increases do not alert
  because alert state is grouped by `duplicate_key`, and alerting on every
  change would ping-pong between two copies of the same trip priced differently.
- **Sorting (`tanio`):** re-checked against this file, RECONNAISSANCE.md §11.3,
  §12.4, §13b-c and the saved raw evidence (`data/wakacje-recon/`). Confirmed
  live were only `?tanio` and `?str-2,tanio` **without** an airport flag, and
  both returned only no-flight products (no `departurePlaceCode`). The shape
  combining `tanio` with an airport flag was never found on any page and never
  tested. **Not implemented** — the provider still uses the default
  "Najpopularniejszych" order. Reaching cheaper flight offers needs one small,
  separately authorized live check of a sort+airport shape first (e.g. whether
  the site itself generates such a link on a `?z-lodzi` page).
- `max_pages: 3` and `max_requests: 14` are unchanged.

## 1. Current implementation state (§1-§4 below describe the PRE-§24 architecture; superseded where noted)

**Superseded 2026-09-24 (RECONNAISSANCE.md §24-26):** the per-airport request
flow this section originally described (unfiltered baseline + one paginated
fetch per confirmed airport, 14 requests/cycle) has been replaced by one
confirmed combined search query (all 4 airports + sort + filters in one URL,
4 requests/cycle: `wakacje.py: CONFIRMED_SEARCH_QUERY`). The per-airport facts
below (confirmed slugs, `CONFIRMED_AIRPORT_SLUGS`) remain true as a historical
evidence record — those slugs are exactly the ones embedded in the new combined
query — but they no longer drive separate requests. See RECONNAISSANCE.md §24-26
for the full evidence trail and rationale.

- **Base scope:** `/wczasy/` (the site's own general search — its search box
  literally reads "Dowolny kierunek lub hotel"), not `/lastminute/`. Confirmed
  materially broader (38,216 vs 23,577 matches at measurement time).
  `travel_deal_agent/providers/wakacje.py`, `LISTING_PATH`.
- **Pagination (superseded shape, same mechanism):** now `?<CONFIRMED_SEARCH_QUERY>`
  for page 1, `?str-<n>,<CONFIRMED_SEARCH_QUERY>` for pages 2+ — the same
  page-number-plus-filter comma shape as before, now carrying the whole
  combined query instead of one airport slug — bounded by
  `cfg.get("max_pages", 2)`, with a graceful budget-exhaustion break (mirrors
  `itaka.py`). `config.json` sets `max_pages: 3`, `max_requests: 4`.
  `wakacje.py: fetch()`.
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
- **`config.json` (`providers["wakacje.pl"]`), current values (superseded
  2026-09-24, RECONNAISSANCE.md §26):**
  ```json
  {"enabled": true, "interval_seconds": 3600, "max_pages": 3,
   "max_requests": 4, "timeout_seconds": 15, "cycle_seconds": 120,
   "request_gap_seconds": 5}
  ```
  `max_requests: 4` = robots(1) + 3 pages of the one confirmed combined
  search query (lowered from `14`, which was robots + baseline + 4 airports
  × 3 pages under the old per-airport architecture — see §26).
  `cycle_seconds: 120` was originally raised from `60` under the old
  14-request architecture (RECONNAISSANCE.md §21-§22) and left unchanged
  since — comfortably sufficient for the new, much shorter 4-request scan
  too (not re-derived down, since it was never a blocker at this lower
  count). `filters["accept_incomplete_price_from"] = ["wakacje.pl"]`.
- **`enabled: true`** (operational session F, 2026-09-22) — an explicit,
  separate decision by the project owner, made only after the full live
  validation in §23 (under the old architecture). **The scheduler HAS since
  been run** (2026-09-24, one controlled `run_once(force=True)` cycle, real
  DB writes, real Telegram delivery — RECONNAISSANCE.md §27; see the
  one-line status above and §5 for full detail). Still no `--watch` / 24/7
  unattended run.
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
| 10 | stay length | COVERED | `filters.min_nights`/`max_nights` (nullable); `filtering.matches_criteria` |
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

## 5. Status and next step — updated 2026-09-24 after the first controlled scheduler run

Superseded by RECONNAISSANCE.md §24-27 (this section previously described the
old per-airport architecture as not-yet-run; both the architecture and that
gap are now different — read this version, not any cached copy).

**What happened, in order:** (a) the old per-airport architecture (§1-§4,
14 requests/cycle) was live-validated end to end (RECONNAISSANCE.md §21-§23);
(b) a real business problem surfaced — the project owner's own manual search
found real, cheap, matching offers (Meridian, Alion, Pebbles Resort, Costa
Malaga) that the old architecture's default "most popular" sort never
reached; (c) a real, human-driven browser session plus one bounded,
explicitly authorized live HTTP confirmation established a single combined
search query that reaches those same offers directly, sorted cheapest-first
(RECONNAISSANCE.md §24-25); (d) `wakacje.py`/`wakacje_data.py` were rewritten
around that query, including a price-semantics fix (the query's `za-osobe`
flag means `price` is now per-person directly, not a total to halve) — full
test suite green throughout (RECONNAISSANCE.md §26); (e) one explicitly
authorized, controlled `scheduler.run_once(force=True)` cycle ran only
Wakacje.pl against the real site, real database and real Telegram — 4/4
requests, 30/30 offers normalized with zero parser warnings, 8 matched and
alerted, all real examples above confirmed present (RECONNAISSANCE.md §27).

**Observed, not yet explained, not speculated on:** `departurePlaceCode ==
"WMI"` (Warszawa-Modlin) appeared in the live results even though the
confirmed query has no separate Modlin slug — only the collective
`z-warszawy` flag (RECONNAISSANCE.md §25, §27). WMI is already a configured
business airport (`filters.airports`), so this needed no code change to be
handled correctly; noted here as observed behavior only, in case a future
session wants to investigate *why* `z-warszawy` sometimes surfaces Modlin.

**Not done in this session, left for later, not blocking:**
1. The 6 test failures the `enabled: true` flip caused under the *old*
   architecture (originally noted here) were already fixed as part of the
   §26 rewrite — the full suite is green (1285 passed) with the new
   architecture; not a separate remaining item.
2. `--watch` / unattended 24/7 operation has still never been authorized or
   run — every run so far has been one explicit, controlled cycle.
3. The pre-existing `data/offers.sqlite3` was found to already contain
   Wakacje.pl rows from 2026-09-22/23, predating this section's earlier claim
   that "no production SQLite write has happened" — that earlier claim was
   simply wrong (or referred to a different database file); not investigated
   further this session, not a blocker.

If a *future* live check ever surfaces something unexpected (redirect, mixed
codes, error, CAPTCHA, a `departurePlaceCode` that doesn't match an intended
filter, a new unmapped country, etc.): stop, report exactly what happened
(mirroring the `z-warszawa-chopin` write-up in RECONNAISSANCE.md §17).

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
- **`?tanio` (superseded 2026-09-24, RECONNAISSANCE.md §24-26):** §13c's
  finding (cheapest-sorted results with no airport filter are dominated by
  no-flight offers) is still true and unchanged for that isolated case, but no
  longer means "never queried" — the confirmed combined query (§24) includes
  `tanio` alongside all four confirmed airport filters and other business
  filters at once, live-confirmed to return real, flight-priced, ascending-
  sorted offers. The general rule stands: never combine `tanio` with an
  airport filter (or anything else) beyond this one specific, live-confirmed
  combined query — do not extrapolate to other ad hoc combinations.
- **`price_is_complete` stays `False` for Wakacje.pl.** This is structural
  (no robots-compliant path to a confirmed booking total exists for this
  source), not a temporary gap. Do not attempt to make it `True`.
- **A listing price may only ever alert through the provider whitelist**
  (`filters["accept_incomplete_price_from"]`), never by weakening
  `filtering.matches()`'s check globally, and never by re-adding a
  price-completeness check to `storage.py` that duplicates that whitelist.
- **Never guess an ISO code.** Extending `COUNTRIES` is always additive and
  evidence-gated. Since §0 an unmapped country is *not* rejected — it keeps
  `country=None` and shows the source's country name in `destination`.
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
