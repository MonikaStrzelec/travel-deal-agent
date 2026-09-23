# Travel Deal Agent — project state (handoff for the next session)

> **Note (documentation currency):** the notification-channel narrative below
> (WhatsApp described as the preferred/target future channel, Telegram
> described as "not planned") is historical and no longer reflects the
> project's current state or decisions. As of this writing: **Telegram**
> (official Bot API) is the implemented and active notification channel for
> the MVP; **Discord** is the preferred channel under consideration for a
> future addition after the MVP (not yet implemented); **WhatsApp is not
> planned**. Sources of truth: `AGENTS.md` and `README.md`.

Updated 2026-09-22 (Wakacje.pl MVP completed and live-smoke-tested; first
controlled ITAKA `--watch` live experiments completed; decision made to start
`WhatsAppNotifier` design as the next stage). This file covers the
**whole project** (all four providers, infrastructure, business requirements,
roadmap, and the immediate next step) — kept at this historical path
(`experiments/rainbow_playwright/CURRENT_STATE.md`) because that is where the
project's existing "current state" file already lived; nothing was moved. A future
session may relocate it to the repo root if that turns out to be clearer.

**Later the same day (2026-09-22, offline-only session, no code changes):** the
ITAKA price-completeness question below was resolved. The project owner made a
binding business decision on what price completeness means (§5), and an offline
review against the existing code, fixtures and tests confirmed the current ITAKA
implementation already satisfies that decision exactly — no code changes were
needed, only this documentation was corrected.

**Later still the same day (2026-09-22, scheduler work):** the scheduler now
supports a per-provider randomized polling range
(`interval_min_seconds`/`interval_max_seconds`, backward-compatible with plain
`interval_seconds`), and the CLI's old hard `--watch` block specific to ITAKA was
removed (Rainbow's own block is unchanged). See §6 for exactly what changed,
`config.json` for ITAKA's new 480–900s range, and the new tests in
`tests/test_storage_scheduler.py`/`tests/test_cli.py`. `providers.itaka.enabled`
was still `false` in the committed `config.json` at the end of this scheduler
work: no live request and no `--watch` invocation happened against the real site
as part of this scheduler change itself.

**Later still the same day (2026-09-22, first controlled live ITAKA
experiments + business decision, no code changes):** using a temporary,
non-committed config (not `config.json`), three controlled live experiments were
run against the real ITAKA site: a first controlled `--watch` dry run, a longer
3-cycle natural `--watch` dry run, and a separate diagnostic cycle with
`max_detail_requests=3`. Full results are in §1 (ITAKA); the scheduler
confirmation is folded into §6. All three produced 0 alerts and 0
`price_is_complete=True`, with the existing fail-closed detail-verification
behavior holding throughout — see §1 for exactly why. Based on these results, the
project owner made a binding decision (detailed in §4 and the updated roadmap in
§6): **do not** wait for a first real ITAKA alert before starting
`WhatsAppNotifier` work, and **do not** keep increasing ITAKA traffic to try to
force one. `WhatsAppNotifier` design is now the project's active next stage (§7).
The committed `config.json` was **not** changed by these experiments —
`providers.itaka.enabled` remains `false` there.

**How to use this file:** read this first, then `AGENTS.md` (workflow rules) and
`README.md` (architecture/quick start). No prior conversation history is needed —
this file is written to be a complete, standalone handoff.

**If you are starting a new session right now:** ITAKA price completeness (§5),
the scheduler's randomized-interval support (§6), and the first live ITAKA
`--watch` experiments (§1, §6) are all resolved/implemented/completed. The
decision on the next stage has already been made (§4): start analysis + design
of `WhatsAppNotifier` (§7) — an official Meta WhatsApp Business/Cloud API
integration, design and offline tests only, no real messages sent yet. ITAKA's
own remaining detail-verification gap (§1) does not block this. Everything
before §4 is background/context; §4, the updated roadmap in §6, and §7 describe
the actual next task.

## 1. Provider status

### ITAKA — implemented, disabled by default, HTTP-only — price completeness resolved, `--watch`-eligible 2026-09-22
No longer CLI-forced into manual-only operation (§6) — `--force` for a single
check still works exactly as before, and `--watch` is now also technically
allowed, though `providers.itaka.enabled` stays `false` until a deliberate
decision to start a real continuous run. Plain `urllib`, robots-respecting,
bounded requests. Parses Next.js RSC `__NEXT_DATA__` from public listing pages;
confirms a booking price via one bounded detail-page fetch. **`price_is_complete`
can already be `True` for ITAKA** once that detail-page confirmation succeeds —
see §5 for the binding business-rule definition of price completeness and the
offline review that confirmed the existing code already implements it correctly,
with no code changes required.

`itaka_details.confirm_detail()` sets `price_is_complete=True` only after the
exact listing variant is matched on the detail page (hotel, dates, room, board,
participants, flights, `saleStatus == "available"`) and the operator's confirmed
total (package price + mandatory operator fees, currently TFG/TFP) reconciles
across two independent price fields on the page. An unrecognized mandatory-fee
`type` still fails closed (`price_is_complete` stays `False`). Local mandatory
costs (visa, tourist tax, mandatory entry fee, etc., parsed from the page's
free-text practical information) are kept separately in
`Offer.local_mandatory_costs`, are never counted toward the PLN 1500/person cap,
and are always surfaced in the rendered notification — see §5.

The old CLI block that rejected `--watch` whenever ITAKA was enabled has been
removed (§6) — ITAKA is no longer forced into `--force`-only operation at the
code level. `config.json` now also gives ITAKA its own randomized polling range,
`interval_min_seconds: 480` / `interval_max_seconds: 900` (8–15 minutes), instead
of a fixed hourly `interval_seconds`, matching the project's actual goal of
catching short-lived last-minute deals quickly. `providers.itaka.enabled` stays
`false` in the committed `config.json` — the live experiments below were run
against a temporary, non-committed config, not against this file.

**First controlled live `--watch` dry run (2026-09-22).** Run against the real
ITAKA site with a temporary config: `providers.itaka.enabled=true`, `mock=false`,
Rainbow/TUI/Wakacje.pl all `enabled=false`, `interval_min_seconds=480`,
`interval_max_seconds=900`, `max_detail_requests=1`. Result: 1 cycle completed
cleanly — 3 requests total (`robots.txt` + listing + 1 detail fetch), 15 offers
from the listing, `price_is_complete=True` for 0 offers, 0 matches, 0 alerts,
`failures=0`, `next_run` scheduled ~678s later (within the configured 480–900s
range), no 403/429/CAPTCHA. The process was deliberately stopped before a second
cycle. The first candidate's detail fetch was correctly rejected fail-closed:
missing `rateType` and `transport`.

**Longer natural dry run — 3 full cycles (2026-09-22).** Same temporary config,
run as real `--watch` (no `--force`, no shortened intervals). Cycle delays:
686s, 495s, 882s — all within the configured 480–900s range. Each cycle made
exactly 3 requests and fetched 15 offers; `failures=0` throughout; no
403/429/CAPTCHA. The same first candidate was fail-closed every cycle, for the
same reason (missing `rateType`/`transport`). Across all cycles: 0
`price_is_complete=True`, 0 matches, 0 alerts.

After 4 real fetches total, the resulting SQLite state: 15 unique offers,
`found_at` preserved, `last_seen` updated correctly, exactly one `price_history`
row per offer (prices did not change between cycles), no false duplicates,
`alert_state` and `notifications` both empty. This live-confirms the scheduler
(§6) as stable: correct `next_run` values, no drift or immediate repeats, state
persisted correctly across runs, correct price history and observation
deduplication.

**Diagnostic test — `max_detail_requests=3` (2026-09-22).** One additional
controlled cycle, without `--watch`, with `max_detail_requests=3` and
`max_requests=5` temporarily raised (all other filters/safeguards unchanged).
Result: listing returned 16 offers; 3 distinct detail pages were checked; 0/3
reached `price_is_complete=True`:
- Candidate 1: fail-closed — missing `rateType` and `transport`.
- Candidate 2: fail-closed — missing `rateType` and `transport`.
- Candidate 3: fail-closed — conflicting detail evidence: two data fragments for
  the same `rate_id` had contradictory `price` structures; the code correctly
  refused to guess or merge them.

Totals: 5 requests, no 403/429/CAPTCHA/timeout, 0 matches, 0 alerts,
`failures=0`.

**Conclusion.** The detail-verification gap (missing `rateType`/`transport`, and
now also conflicting evidence for the same `rate_id`) appears at least partially
systemic, not just bad luck on the first candidate. Decision: do not keep raising
`max_detail_requests` diagnostically just to force an alert (see §4). Recommended
starting budget going forward: `max_detail_requests=2` as a cautious compromise —
a recommendation, not a hard-proven optimum. This detail-verification coverage
gap is now tracked as an ITAKA follow-up in the updated roadmap (§6) but does
**not** block the next stage (§7).

### Rainbow — MVP works technically, disabled by default, Playwright — not a current priority
Listing collection plus bounded, passive detail-page enrichment: the browser's own
listing-page JSON responses are passively captured (never crawled directly), joined
to one visible card, and at most `max_detail_requests` candidates get one bounded
detail-page fetch to confirm the exact selected configuration (airport, board,
dates, price) — never a fabricated airport×meal combination. `price_is_complete`
stays `False` always (mandatory-cost verification remains unresolved), so Rainbow
cannot produce a real alert yet. Live-tested successfully (200 OK, no blocks); the
production-filtered search returned 0 offers, a legitimate business result at the
strict PLN 1500/person cap. Full history: `VARIANT_FINDINGS.md` in this same
directory. Not touched this session. Deliberately **not** the next priority (see
§4/§5) — ITAKA's price completeness is now resolved (§5); Rainbow's own
price-completeness work resumes only after the roadmap items in §6.

### TUI — MVP works technically, disabled by default, Playwright + passive capture — not a current priority
**Final architecture:** we never call `/api/` manually.
`tui_query.build_search_path(filters)` builds the confirmed
`/wypoczynek/wyniki-wyszukiwania-samolot?q=...` URL from the shared `FilterConfig`
(airports, party size, price cap ×2 for `amountRange`, board codes, min-stars code,
sort, the one confirmed `dF:6:dT:14` duration envelope). `tui_browser.py`
(`capture_search_offers`) opens that URL in a fresh headless Chromium instance and
passively listens for the page's own, naturally-triggered response:
`GET https://www.tui.pl/api/services/tui-search/api/search/offers` — exactly one
match is required (zero → `TuiTimeout`, more than one → `TuiStructureError`;
matched by host **and** path, never guessed). No offer card is opened, no
scrolling, no manual `/api/` request. `tui_data.py` parses two confirmed source
shapes through one shared `normalize_offer(..., *, source)`:
- `category_ssr` — SSR `__NEXT_DATA__` embedded in
  `/wypoczynek/<country>/oferty-last-minute`; duration rule: `date_diff == duration + 1`.
- `search_xhr` — the passively captured response body (**production** source);
  duration rule: `date_diff == duration` (confirmed different from category_ssr;
  no shared "magic" rule was attempted).

`hotelStandard` accepts `float`, including genuine half-star values (3.5, confirmed
live). `price_is_complete=False` and `variant_verified=False` always, regardless of
source — never raised to `True` just because a record is specific.

**Final live smoke test (2026-09-22): SUCCESS.**
- Exactly one capture call, exactly one matched `search/offers` response.
- TUI returned **13 offers**; the parser normalized **13/13** (100%, zero rejected
  records).
- `hotelStandard` float confirmed live (values seen: 3.0, 3.5, 4.0).
- No 403/429/CAPTCHA.
- `filtering.matches()` correctly rejected all 13: 8 were outside 7–9 days
  (confirmed rejected), and the remaining 5 (within 7–9 days) were still rejected
  via `price_is_complete=False` — **zero alerts generated**, exactly as designed.

Provider stays **disabled by default**: `config.json` → `"tui": {"enabled": false,
"interval_seconds": 3600, "timeout_seconds": 20}`. Not registered as a default
scheduler source beyond `registry.py`'s existing `"tui"` branch (same
disabled-unless-configured pattern as ITAKA/Rainbow).

Files: `travel_deal_agent/providers/tui.py`, `tui_data.py`, `tui_query.py`,
`tui_browser.py`, `tui_errors.py`. Tests: `tests/test_tui*.py` (parser, query
builder, provider, browser capture) plus fixtures under `tests/fixtures/tui/`.
Not a current priority (see §2 follow-ups below, and §4/§5 for why ITAKA goes
first) — TUI's `price_is_complete` gap is a single-document, unconfirmed-detail
problem that hasn't been re-attempted this session.

### Wakacje.pl — MVP complete for the confirmed WRO scope, disabled by default, HTTP-only
Listing-only provider (`travel_deal_agent/providers/wakacje.py`,
`wakacje_data.py`), no Playwright, no detail-page fetches at all — reconnaissance
this session (`experiments/wakacje_pl/RECONNAISSANCE.md`) established that
Wakacje.pl's bare, robots-legal detail page carries **no offer-specific data**, so
unlike ITAKA there is no detail-confirmation stage to build here; `price_is_complete`
stays `False` unconditionally, for a structural reason (no legal path to it), not a
temporary one.

**Architecture:** parses the standard Next.js `__NEXT_DATA__` embedded in
`/lastminute/` (plain HTTP, robots-checked, `RequestBudget`-bounded, same shape as
`ItakaProvider`). A confirmed, real, robots-legal single-flag departure filter
(`/lastminute/?z-wroclawia`) makes every returned offer unambiguously priced for
that one airport — this replaces the detail-page confirmation step other providers
use for the same ambiguous-card problem (a numeric offer `id` alone was proven
**not** stable across airport/date context: the same `id` showed a different date
and price under `?z-wroclawia` vs. the unfiltered listing). Variant identity is a
sha256 digest over `{source_id, departure_airport_code, departure_date, duration,
service}` — never the raw numeric `id` alone.

**Only WRO has a confirmed departure-filter slug** (`z-wroclawia`). LCJ/WAW/KTW do
**not** — no confirmed slug exists for them anywhere in reconnaissance evidence,
and none was guessed (`CONFIRMED_AIRPORT_SLUGS` in `wakacje.py` intentionally has
exactly one entry). Those airports are only reachable by chance via the unfiltered
baseline fetch, never deliberately queried.

**Final live smoke test (2026-09-22): SUCCESS.**
- Three requests total: `robots.txt`, baseline `/lastminute/`, filtered
  `/lastminute/?z-wroclawia` — all `200 OK`. No 403/429/CAPTCHA.
- Baseline listing: 10 raw offers, 7 normalized (3 rejected, fail-safe — see edge
  cases below). WRO-filtered listing: 10 raw offers, 7 normalized (same failure
  shapes). **14 offers total** returned by `provider.fetch()`.
- Confirmed live: every WRO-filtered offer has `departure_airport == "WRO"`;
  `price_per_person == total_price / 2` for all; `price_is_complete is False` for
  all 14; variant IDs deterministic across a repeated offline re-parse of the same
  captured body; `hotel_stars` observed as float including a genuine half-star
  value (3.5) live; board mapping confirmed live **only for AI** (this
  last-minute category skewed heavily AI — HB/BB/FB mapping is covered by offline
  unit tests only, not exercised by this particular live pull).
- `filtering.matches()` correctly rejected all 14 via `price_is_complete=False` —
  **zero alerts generated**, exactly as designed. 0 offers would also be a valid
  business outcome per this project's convention; here we got 14, all correctly
  non-alerting.

**New edge cases discovered live, not previously known from reconnaissance** (not
fixed — not blockers, fail safely today):
- Some raw records have a `duration` inconsistent with the `departureDate`/
  `returnDate` difference (the parser's own consistency check correctly rejects
  just that one record, not the whole page).
- Some raw records are missing `place.city` entirely (Pydantic validation
  correctly rejects just that one record).

Neither is a blocker for the current WRO-only scope (`parse_listing`'s per-record
try/except already isolates them, matching ITAKA/TUI's own established fail-safe
pattern) — worth a closer look only if it starts materially reducing offer
coverage, not before.

**Business follow-up, not yet investigated (2026-09-22).** The project owner's
own manual search on Wakacje.pl's general search ("Dowolny kierunek lub hotel")
found real offers below PLN 1500/person. The current provider is confirmed only
against `/lastminute/` and the WRO-filtered variant of it — the current MVP's
coverage may therefore be narrower than what the site's general/broad search
surfaces. This needs investigating later (not now): whether the general-search
listing is a distinct, robots-legal, parseable source, and whether it should be
added alongside or instead of the current `/lastminute/`-based scope. **Not
investigated or implemented this session** — noted here only as a known gap; see
the updated roadmap in §6.

Files: `travel_deal_agent/providers/wakacje.py`, `wakacje_data.py`. Tests:
`tests/test_wakacje.py`, `tests/test_wakacje_data.py` (103 tests) plus fixtures
under `tests/fixtures/wakacje/`. Full reconnaissance history, including all
robots.txt findings, the SSR/filter-catalog analysis and both authorized live
passes: `experiments/wakacje_pl/RECONNAISSANCE.md`.

## 2. TUI follow-ups (known, not blockers — not fixed on purpose this session)

- **Price completeness.** Every TUI offer has `price_is_complete=False`, so
  `filtering.matches()` intentionally never lets one through to a real alert
  (mirrors Rainbow's and Wakacje.pl's same still-open requirement; ITAKA's
  equivalent requirement was resolved 2026-09-22, see §5). Needed before TUI
  can ever produce a production alert.
- **One missing board alias.** A live record had `boardType="Dwa posiłki plus"`
  with `boardCode="GT06-HBP"` (already correctly code-mapped to HB); the *title*
  alias is missing from `boards.py`'s `_NAMES["HB"]`, so `board_type` came back
  `None`. Fails safe (offer just doesn't match `allowed_boards`); worth adding
  `"dwa posiłki plus"` as an HB alias later.
- **Richer flight data unused.** `search_xhr` responses include `departureFlight`/
  `returnFlight` (flight number, airline, exact times) — not extracted yet;
  explicit future enhancement, not required now.
- **Country map gap.** `COUNTRIES` in `tui_data.py` doesn't include "Malta" —
  observed live (`country=None` for Malta hotels). Fails safe; extend when
  convenient.

## 3. Current business requirements (production `config.json`)

- 2 adults, 0 children, 1 room.
- Max price per person: **configurable**, currently PLN 1500.
- Airports: **LCJ (Łódź) is the clear priority**; also WAW, KTW, WRO. (Rainbow's own
  site additionally supports WMI; TUI's does not — confirmed disabled there.)
- Stay length: **7–9 days**, currently and always enforced only by the shared
  `filtering.matches()`, regardless of what any single upstream source's own
  filters return (each provider's upstream range is a superset at best).
- Minimum 3★; relevant African destinations (plus a handful of Eastern European
  ones) require minimum 4★ — see `filters.country_min_stars` in `config.json`.
- Board: AI / FB / HB (All Inclusive / 3 meals / 2 meals).
- Rating + review count feed ranking/quality — never an undisclosed hard filter
  beyond what's explicitly configured per provider.
- Ranking: weighted, normalized **0–100** score (`ranking.py`).
- Price history: SQLite (`storage.py`); detects new offers, price drops, and the
  lowest price an offer has ever alerted at (`alert_state.lowest_alert_price` only
  ever ratchets down, never up — see `storage._enqueue_alert`/`alerts.classify_alert`).
- Alert deduplication: no repeat alert for the same offer at the same price
  baseline (`alerts.py`, `storage.py`'s `alert_state` table keyed by
  `duplicate_key(offer)`).
- Notification channel: **WhatsApp is the target**, not yet implemented (§7).

## 4. Key blocker — three providers are still gated on `price_is_complete`

`filtering.matches()` (`travel_deal_agent/filtering.py`) requires
`offer.price_is_complete` to be `True` before an offer can ever be eligible; and
`OfferPipeline.finalize()` only enqueues an alert (`store.observe(..., eligible=True)`)
for offers that already passed `matches()`. **Rainbow, TUI and Wakacje.pl still
return `price_is_complete=False` unconditionally**, so those three cannot generate
a real alert yet.

**ITAKA is the exception.** `confirm_detail()` already sets
`price_is_complete=True` once a listing variant's detail page is fetched and
confirmed (see §1, §5) — ITAKA can produce a real, non-duplicate alert today for
any offer whose confirmed operator price clears the configured cap, subject only
to the existing detail-request budget (`max_detail_requests`) that bounds how many
listing rows get a detail fetch per cycle. As of §6, ITAKA is also no longer
CLI-blocked from `--watch` — only `providers.itaka.enabled: false` in
`config.json` stands between today's state and a real continuous run, and that
flag has been left `false` deliberately, pending a separate decision to actually
start one.

This gate exists on purpose — it is the project's guarantee that an alert is only
ever sent for a price that has actually been confirmed complete (not a "from"
price missing mandatory fees). **Do not remove or work around this check to make
alerts "start working"** for Rainbow, TUI or Wakacje.pl. The only correct fix for
those three is the same one already done for ITAKA: prove that a specific set of
conditions makes `price_is_complete=True` safe to set for that source, following
the binding business-rule definition of price completeness in §5.

**Decision (2026-09-22, after the live ITAKA experiments in §1):** three
controlled live experiments (a first `--watch` dry run, a longer 3-cycle natural
dry run, and a `max_detail_requests=3` diagnostic — full results in §1) all
produced 0 offers with `price_is_complete=True`, with the same fail-closed
detail-verification gap (missing `rateType`/`transport`, plus one case of
conflicting detail evidence for the same `rate_id`) recurring across candidates.
Based on this, the project owner decided:
- The project will **not** wait for a first real ITAKA alert before starting
  work on the notification channel.
- ITAKA traffic will **not** be increased further for the purpose of forcing an
  alert (no further diagnostic bump of `max_detail_requests`).
- The existing fail-closed behavior is preserved exactly as-is — this decision
  does not weaken `filtering.matches()` or the `price_is_complete` gate.
- The `rateType`/`transport`/conflicting-evidence detail-verification gap is
  tracked as an ITAKA follow-up in the updated roadmap (§6) but does **not**
  block the next stage.
- The next stage is `WhatsAppNotifier` (§7): first an analysis of the existing
  notification infrastructure, then a design for a minimal integration with the
  official Meta WhatsApp Business/Cloud API. No real messages are sent yet.

## 5. Price-completeness business rule (binding) — ITAKA resolved 2026-09-22

**Binding project policy, decided by the project owner 2026-09-22.** This is the
project-wide definition of price completeness; it governs every provider, not just
ITAKA:

1. The configurable per-person price cap (currently PLN 1500) applies to the
   **confirmed booking price charged by the operator/seller**.
2. Every mandatory fee the operator charges to complete the booking must be
   included in that price. For ITAKA this means TFG and TFP.
3. Mandatory **local** costs (visa, tourist tax, mandatory entry fee, other
   locally-paid charges) are **not** added to the PLN 1500/person cap and do not
   by themselves disqualify an offer.
4. Local costs must still be shown very clearly in the notification.
5. If a possible or mandatory local cost is known but its exact amount is not
   certain, the notification must never imply there is no cost — it must say so
   explicitly (e.g. "Possible additional local costs — amount unconfirmed").
6. `price_is_complete=True` means the **booking price charged by the
   operator/seller is complete** — it does **not** mean the total cost of the
   whole trip including all local costs.
7. This does not weaken `filtering.matches()` or bypass the `price_is_complete`
   gate.

**Verified 2026-09-22 (offline review only — no live requests, no code changes):
the existing ITAKA implementation already satisfies this rule exactly.**

- `itaka_details.confirm_detail()` sets `price_is_complete=True` only after the
  specific rate/variant from the listing is matched against the detail page
  (hotel, dates, room, board, participants, flights) and its
  `saleStatus == "available"`.
- The operator price it confirms (`package_price` + `operator_mandatory_fees`,
  reconciled into `booking_total_price`/`total_price`/`price_per_person`) includes
  every mandatory operator fee the code currently recognizes — TFG and TFP —
  cross-checked against two independent price fields on the detail page
  (`variant.price.actualWithAdditionalPayments` and the separate
  `summary.totalPrice` object).
- An unrecognized or duplicated mandatory-fee `type` in `additionalPayments`
  still fails closed: `fee_totals()` raises, `confirm_detail()` raises, and the
  offer is returned with `price_is_complete=False` and a
  `price_verification_reason` explaining why (`itaka.py`'s exception handler).
- Local mandatory costs detected from the page's free-text `local_information`
  (e.g. Egypt visa ~30 USD, Cabo Verde entry fee ~30.9 EUR/person, tourist tax
  ~2.5 EUR/night — all observed in the committed fixtures) are kept in
  `Offer.local_mandatory_costs`, separate from `price_per_person`/`total_price`,
  and are **not** considered by `filtering.matches()` — matching rule 3 above.
- Local costs are always surfaced in the rendered notification
  (`notification_content.py`): each known cost is listed with its `certainty`
  (`exact`/`approximate`/`unknown`); when none were detected, the message says
  "Local mandatory costs: no data (does not mean no costs)" instead of implying
  there are none — matching rules 4 and 5 above.

No code or test changes were made as part of this verification — the existing
implementation and test suite (`tests/test_itaka_details.py`, in particular
`test_captured_booking_prices_and_legacy_identity`,
`test_local_costs_do_not_change_eligibility_or_price` and
`test_confirmation_preserves_history_and_alert_baseline`) already pin exactly
this contract.

**Remaining open item (non-blocking — does not gate the current definition):** a
one-time manual walkthrough of ITAKA's real checkout, up to but not including
payment, to confirm no further mandatory operator fee appears only at that stage
(something no public detail-page fixture captured so far could reveal). This is
optional additional confirmation, not a prerequisite — the current
`price_is_complete=True` behavior already matches the binding business rule above
without it. If ever done, it requires the same explicit-authorization,
minimal-request discipline used throughout this project (see §9) — propose it,
don't just do it.

## 6. Scheduler: randomized polling interval + ITAKA `--watch` unblocked (implemented 2026-09-22)

**Binding scheduling decision from the project owner:** for a normal, successful
cycle the project wants a configurable **random** delay per provider, not a fixed
interval plus a purely additive jitter. The project's main goal is catching
short-lived last-minute deals quickly, so a perfectly periodic, hour-long cadence
was rejected in favor of this model:

- Per-provider config: `interval_min_seconds` / `interval_max_seconds`.
- On every **successful** cycle, the scheduler draws exactly once:
  `next_delay = uniform(interval_min_seconds, interval_max_seconds)`, then sets
  `next_run = now + next_delay`. Equal bounds give an exact interval with no draw.
- **Backward compatible:** `interval_seconds` remains a required field with its
  original meaning. A provider configured with neither range field behaves
  exactly as before — `next_run = now + interval_seconds`, fully deterministic.
  A config with only one end of the range set is rejected at startup
  (`0 < interval_min_seconds <= interval_max_seconds`, both-or-neither, never a
  partial range).
- **Backoff is untouched:** a failed cycle always uses the pre-existing
  deterministic exponential backoff (`interval_seconds * 2**failures`, capped by
  `scheduler.max_backoff_exponent`). The random range is never consulted on that
  path, so retry timing after an error stays fully deterministic and is never
  diluted by randomness — this was an explicit requirement, not an oversight.
- **Per-cycle protections are independent and unchanged:** `robots.txt`,
  crawl-delay, `request_gap_seconds`, `cycle_seconds` and each provider's own
  request budget are a separate, lower-level mechanism (`providers/http.py`'s
  `RequestBudget`) untouched by this change. The randomized range only spaces out
  *cycles*; it never loosens *intra-cycle* pacing, and a source needing a more
  cautious cadence can simply be given a larger range. This is a traffic-shaping
  mechanism, not a way to evade a site's protections.
- **CLI:** the old hard block that rejected `--watch` whenever ITAKA was enabled
  was removed from `__main__.py`. Rainbow's equivalent block is unchanged — its
  own price completeness is still unresolved (§4/§5), so it stays CLI-rejected
  under `--watch` unconditionally.
- **Config:** `config.json` → `itaka` now has `interval_min_seconds: 480` /
  `interval_max_seconds: 900` (8–15 minutes) alongside the pre-existing
  `interval_seconds: 3600` (kept unchanged, now used only as the deterministic
  backoff base). `providers.itaka.enabled` stays `false`. Rainbow, TUI and
  Wakacje.pl are unchanged (`enabled: false`, no range fields added — pointless
  while their own `price_is_complete` stays unconditionally `False`; do not
  enable them just because the scheduler can now technically run them).

**Changed files:** `travel_deal_agent/config_types.py` (new optional
`interval_min_seconds`/`interval_max_seconds` on `ProviderConfig`),
`travel_deal_agent/config.py` (`validate_options` — both-or-neither, range
bounds), `travel_deal_agent/scheduler.py` (`Scheduler.__init__` gains an
injectable `random_range` callable defaulting to `random.uniform`;
`Scheduler._next_delay()` implements the fallback/range logic; only the
success-path `schedule()` call in `_fetch()` uses it — backoff and the pre-fetch
crash-safety reservation are untouched), `travel_deal_agent/__main__.py` (ITAKA
watch-block removed), `config.json` (ITAKA's new range). **Not changed:**
`filtering.py`, `pipeline.py`, `models.py`, `storage.py` schema/`schedule()`/
`run_state()`, any provider's HTTP budget/robots/crawl-delay logic, ITAKA's price
logic, Rainbow/TUI/Wakacje.pl code.

**New tests:** `tests/test_storage_scheduler.py` gained coverage for: a
deterministic injected RNG being called with the exact configured bounds; equal
min/max giving the exact interval without even calling the RNG; a provider with
no range fields keeping the old `interval_seconds`-only behavior (with a
RNG that raises if called, proving it's never invoked); a simulated restart
honoring the already-persisted `next_run` without drawing a new random value;
backoff after a failure ignoring the configured range entirely (same RNG-raises
guard); provider failure isolation still holding with range-configured
providers; and an empty offer list still counting as a successful cycle. New
`tests/test_cli.py`: `--watch` with ITAKA enabled no longer raises, `--watch`
with Rainbow enabled still does.

**Quality gate:** pytest 998/998 passed (was 986 before this change — 12 new
tests); Ruff lint clean; Ruff format clean; strict mypy clean (68 source files
under `[tool.mypy] files = ["travel_deal_agent", "tests"]`).

**Done since (2026-09-22, later the same day — see §1 for full results):** the
first controlled `--watch` dry run against the real site, plus a longer 3-cycle
natural `--watch` dry run, were both executed using a temporary, non-committed
config (`providers.itaka.enabled=true` only in that temporary config). The
committed `config.json` was **not** changed by these experiments —
`providers.itaka.enabled` remains `false` there. Both runs live-confirmed the
scheduler as stable: cycle delays consistently fell inside the configured
480–900s range, `next_run` was correct with no drift or immediate repeats, and
state (offers, `found_at`/`last_seen`, `price_history`, `alert_state`,
`notifications`) persisted correctly across the runs. A separate diagnostic
cycle with `max_detail_requests=3` was also run (§1); see §4 for the resulting
business decision.

**Roadmap (updated 2026-09-22, after the live ITAKA experiments in §1 and the
decision in §4):**
1. `WhatsAppNotifier` — design + offline tests: analyze the existing
   notification infrastructure (`Notifier`, outbox, `notification_content.py`)
   and design a minimal integration with the official Meta WhatsApp
   Business/Cloud API. No real messages sent yet.
2. A safe, deliberate test of the notification channel itself.
3. Docker (Dockerfile + `docker-compose.yml`).
4. 24/7 VPS deployment.
5. Longer production operation.
6. Only **after** all of the above, resume provider follow-ups: ITAKA
   detail-verification coverage (§1, §4), Rainbow/TUI/Wakacje.pl price
   completeness, and extending Wakacje.pl beyond its current Last
   Minute/WRO scope (§1's business follow-up note).

This **supersedes** the previous ordering (a longer `ConsoleNotifier` dry-run →
Docker → `WhatsAppNotifier` → VPS → provider follow-ups): the longer
real-traffic dry-run step has already been completed (§1) and is no longer a
prerequisite for starting `WhatsAppNotifier` work.

## 7. Notifications

Preferred channel **at the time this section was written** (superseded — see
the note at the top of this file; Telegram is now the actual MVP channel, and
Discord, not WhatsApp, is the preferred channel under consideration for a
future addition): **WhatsApp** (not yet implemented). `ConsoleNotifier`/local
logging is the current, working delivery mechanism (`travel_deal_agent/notifications.py`).
Message content is already transport-independent: `NotificationMessage` (in
`notification_content.py`) is built from an immutable outbox snapshot and carries
`notification_id` for future delivery idempotency — a `WhatsAppNotifier` should
only need to implement `Notifier.send()`, no changes to `Notifier`, the outbox, or
`classify_alert()`. See README's "Notification channels and content" section.
(Historical: Telegram was considered not planned at the time this was
written; it has since become the primary, implemented MVP channel. Discord,
not WhatsApp, is now the preferred channel under consideration for a future
addition — see the note at the top of this file and `AGENTS.md`/`README.md`.)

**Status update (2026-09-22): this is now the active next stage of the
project** — see §4 for the decision and §6 for the updated roadmap. The
immediate task is analysis + design only: review the existing
`Notifier`/outbox/`notification_content.py` infrastructure and design a minimal
`WhatsAppNotifier` against the official Meta WhatsApp Business/Cloud API. No real
WhatsApp messages have been sent; no `WhatsAppNotifier` code exists yet.

## 8. Infrastructure state

**Already implemented and working** (verify by reading the code, not by assuming):
- Per-provider scheduling with persistent due times across restarts
  (`scheduler.py`'s `Scheduler`, `storage.py`'s `provider_runs` table).
- Exponential backoff on provider failure, capped by
  `scheduler.max_backoff_exponent` (`Scheduler._fetch`).
- Failure isolation per provider — one provider's exception doesn't stop the
  cycle for the others (`Scheduler._fetch`'s `except Exception` boundary; only
  database failures are allowed to propagate).
- `price_history` table, one row per observed price change (`storage.py`).
- `alert_state` table for alert deduplication, keyed by `duplicate_key(offer)`
  (`models.py`/`storage.py`).
- Transactional notification outbox: `notifications` table,
  `Store.pending()`/`Store.mark_delivered()`, `notifications.deliver_pending()` —
  a notification is only marked delivered after a successful `Notifier.send()`,
  so a delivery failure retries next cycle without duplicating already-sent ones.
- `alerts.classify_alert()`: pure function distinguishing `new_offer` vs.
  `price_drop` (cumulative drop below the last alert baseline) vs. no alert.
- `Notifier` ABC + working `ConsoleNotifier` (alias `LogNotifier`).
- `NotificationMessage` + `notification_id` — transport-independent message
  content, ready for a future channel.
- The full `OfferPipeline`/`filtering.matches()` eligibility path (§4).
- Per-provider **randomized polling range** (`interval_min_seconds`/
  `interval_max_seconds`, backward-compatible with plain `interval_seconds`,
  never applied to backoff) — implemented 2026-09-22, see §6.

**Not yet implemented:**
- `WhatsAppNotifier` (deferred by design — §7).
- `Dockerfile` / `docker-compose.yml` — none exist in the repo yet.
- VPS deployment — not started.

## 9. Repository / process rules

- **Local repo only. Do not push to GitHub. Do not add a git remote.**
- Zero commits exist in this repo so far (everything is untracked); nothing was
  committed or pushed this session either.
- Respect `robots.txt` and rate limits for every source; prefer HTTP over
  Playwright, use Playwright only when actually necessary (confirmed by
  reconnaissance, not assumed); never circumvent a CAPTCHA or access block.
- Never guess offer or variant data (airport slugs, filter parameters, price
  semantics, field meanings) — confirm from evidence or leave it explicitly
  unresolved, as this session's Wakacje.pl work did for LCJ/WAW/KTW slugs.

## 10. Quality gate

Latest full run, this session, **after** the scheduler randomized-interval change
(§6): **pytest 998/998 passed; Ruff lint PASS; Ruff format --check PASS; strict
mypy PASS (68 source files).** (Previous full run, after the Wakacje.pl MVP:
pytest 986/986; before Wakacje.pl: 883/883.) The ITAKA price-completeness
business-rule review (§5) in between was documentation-only — no code or test
changes, so that step didn't move these numbers; the scheduler change (§6) is
what took pytest from 986 to 998 (12 new tests: 10 in
`tests/test_storage_scheduler.py`, 2 in the new `tests/test_cli.py`) and the
source-file count from 67 to 68 (the new `tests/test_cli.py`).

**2026-09-22 update:** this session's work (live ITAKA `--watch` experiments in
§1 against a temporary, non-committed config, plus this documentation update)
did not modify any source or test file, so the numbers above remain the latest
committed quality-gate result. No test suite run was performed as part of the
live experiments or this documentation update.

## 11. Diagnostics on disk (gitignored, not committed, useful for offline re-analysis)

- `data/tui-production/` — TUI recon HTML, the Playwright POC capture, and the
  final smoke-test's captured `search/offers` response body.
- `data/rainbow-production/` — the equivalent Rainbow live-run evidence.
- `data/wakacje-recon/` — Wakacje.pl reconnaissance HTML/`__NEXT_DATA__` extracts
  from all three sessions (unfiltered listing, `?z-wroclawia` listing, and the
  bare detail-page fetch that was confirmed to carry no offer data).

## Collaboration preference

Communicate with the owner in Polish; keep code and documentation in English.
