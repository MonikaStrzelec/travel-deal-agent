# Wakacje.pl — reconnaissance (2026-09-22)

One offline-analyzed live pass, per session budget. No provider code written yet.
Raw evidence saved to `data/wakacje-recon/` (gitignored): `headers.txt`,
`lastminute.html` (905 KB), `next_data.json` (extracted `__NEXT_DATA__` blob).

**Session 2 (same day, still 2026-09-22): a second, purely offline pass over this
same saved evidence** dug much deeper into `next_data.json` and the raw HTML —
no new network request was made. It resolved several previously-open unknowns
and sharpened others. See §11 for the full evidence trail; §8 (unknowns) and
§10 (next step) below are updated to reflect it. Read §11 before acting on §4–§9,
since some of those sections' original conclusions are refined there.

**Session 3 (same day, still 2026-09-22): the two authorized bounded live GETs
from §10's plan were executed** — `GET https://www.wakacje.pl/lastminute/?z-wroclawia`
and `GET https://www.wakacje.pl/oferty/turcja/wybrzeze-egejskie/didim/laur-experience-elegance-211281.html`.
Both returned `200 OK`, no CAPTCHA/block, no further requests made (no
pagination, no combined filters, no retries, no Playwright). Full results in
§12 — they resolve §8b.3 and §8b.4 decisively, and **change the recommended
architecture**: a bounded detail-page confirmation step is no longer viable at
all for Wakacje.pl (§12.2), so §9's architecture is rewritten as listing-only
(§12.4 has the reasoning). Read §12 before §9 for the current recommendation.

## 1. `robots.txt`

Fetched `https://www.wakacje.pl/robots.txt` (one read, not counted against the
single listing GET). Relevant to `User-agent: *` (no separate rule set applies to
us — the `ChatGPT-User` block is more permissive but irrelevant):

- **No `Crawl-delay` directive anywhere in the file** (checked both UA blocks).
- **Disallowed, relevant to this project:**
  - `/*,*/ ` — any path with a comma-separated segment (Wakacje.pl's classic
    filtered-category URLs, e.g. `country,filter/`, use this shape).
  - `/oferty/*?*` — query-string variants of detail pages under `/oferty/`
    (plain `/oferty/.../slug-id.html` with **no** query string is **not** covered).
  - `/ajax/`, `/*.json` — the internal API surface (see §5) is explicitly
    off-limits to direct polling.
  - `/*?od-*`, `/*?do-*`, `/*,od-*`, `/*,do-*`, `/*-malejac*`, `/*-rosnac*`,
    `/*?bez-*` (except four explicit `Allow:` exceptions below), `/*przecena*`,
    `/*?statid*` — most query-string/segment filter and sort variants.
  - `/lastminute/*europa-h*`, `/wczasy/*europa-h*`, `/narty/*europa-h*` — one
    specific pattern, not a blanket ban on those sections.
  - `/rezerwacja/`, `/platnosc/`, `/partner/`, `/hoteldata/`, `/opinie-ocen/`,
    and others irrelevant to listing/search.
- **Explicitly allowed (exceptions carved out of the broader `?bez-*` ban):**
  `/wczasy/?bez-paszportu`, `/wczasy/?str-*bez-paszportu`,
  `/wczasy/?bez-testu-na-covid-19`, `/wczasy/?bez-testow-dla-zaszczepionych`.
- **Plain, unfiltered category listings** (e.g. `/lastminute/`, `/wczasy/`, with
  no query string and no comma segment) are **not disallowed** — this is what was
  fetched.

**Conclusion:** a plain category listing page is legal to fetch. Most
*filtered* listing URLs (by airport, price, sort, etc. via query string or comma
segments) appear to be deliberately excluded from robots.txt — see §7 for what
this means for filtering.

## 2. Listing URL fetched (the one GET for this session)

```
GET https://www.wakacje.pl/lastminute/
```

- `HTTP 200`, `Content-Type: text/html; charset=utf-8`, 905,726 bytes.
- No CAPTCHA, no block, no unusual headers. Response cached at the edge
  (`x-cache-status: HIT`), server is nginx.
- Response body is real UTF-8 (verified at byte level); any mangled Polish
  diacritics seen during offline analysis were a **terminal display artifact
  only** (Windows console codepage), not a data problem — `Wybrzeże` round-trips
  correctly as UTF-8 bytes `c5 bc` for `ż`.

## 3. Data source: confirmed SSR JSON, no client-side JS required

The page embeds a standard **Next.js `__NEXT_DATA__`** script tag (same family
of mechanism as ITAKA's RSC data and TUI's `category_ssr` shape — see
`itaka_rsc.py` / TUI's `CURRENT_STATE.md` for the established pattern this
matches). Structure:

```
props.dehydratedState.queries[]  — a React Query dehydration array, 5 entries:
  [0] "header-content"                     — site chrome, irrelevant
  [1] "listingOffers" (+ full search params as part of the key) — THE OFFER DATA
  [2] "seo-listing-text[/lastminute/]"      — SEO copy, irrelevant
  [3] "internal-linking"                    — null in this response
  [4] "banners"                             — marketing, irrelevant
```

Query `[1]`'s key embeds the **exact internal search request** that produced the
SSR data (method `search.tripsSearch`, see §5), and its `state.data` holds:

```
offers.data   — list of offer records actually used to render the cards (10 of them)
offers.count  — total matching count for this (unfiltered) search: 23,792
offers.searchObj, offers.qsSegment, offers.isSortedByQs — internal, not needed
```

**No Playwright is needed to reach this data.** It is present, complete, and
structured in the plain SSR HTML of a page allowed by robots.txt. This is the
same "prefer HTTP + embedded JSON" outcome as ITAKA and TUI's `category_ssr`
route (TUI's *filtered* route was the one that needed Playwright — see §7 for
the open question of whether Wakacje.pl has an analogous filtered-route gap).

Separately, the page also carries three `application/ld+json` blocks
(`BreadcrumbList`, `ItemList`, `FAQPage`). The `ItemList` block independently
confirms each offer's canonical detail-page URL (see §6) but carries no pricing
or offer detail — `__NEXT_DATA__` is the authoritative source, JSON-LD is a
secondary confirmation only.

## 4. Fields available without Playwright (from one sample offer)

All of the following were present per offer record in `offers.data`:

| Business field | Source field(s) | Notes |
|---|---|---|
| Hotel | `name`, `hotelId` | |
| Source offer ID | `id` / `offerId` (same value) | |
| Country / region / city | `place.country.name`, `place.region.name`, `place.city.name` | Polish names, plus `slug`/`urlName`/`id` per level |
| Price | `price` (+ `priceDiscount`, `priceOld`) | **Resolved as total for the party, high confidence — see §8a.1** |
| Currency | `originalCurrency`, `shownCurrency` | both `"PLN"` in sample |
| Hotel stars | `category` (also `maxCategory`) | float-capable — saw `4.5` live, matches TUI's confirmed float-stars precedent |
| Rating | `ratingValue` / `ratingString` | **0–10 native scale** (saw 7.1–9.0) — distinct scale from ITAKA/TUI, must stay in its own native-scale field per `AGENTS.md` |
| Review/recommend count | `ratingReservationCount` (`ratingRecommends` duplicates it) | **Field name says "reservation count," semantics vs. "review count" still unconfirmed — see §8b.1** |
| Board | `service` (numeric code) + `serviceDesc` (free text) | **Numeric code is a reliable AI/HB/BB/WŁ/ZO/FB bucket — see §8a.2** |
| Departure date / return date | `departureDate`, `returnDate` | ISO `YYYY-MM-DD` |
| Duration | `duration` (`durationNights` duplicates it) | nights, matches `return − departure` in the sample |
| Departure airport (priced) | `departurePlace`, `departurePlaceCode` (IATA-style, e.g. `RZE`, `KTW`, `WRO`) | this is the **one airport the shown price is for** |
| All airports this hotel/date can depart from | `departurePlaces` (list of city names, no codes) | **not priced individually — see §6, this is the key variant-ambiguity finding** |
| Departure time | not present in this record | `departureType`/`departureTypeName` only says "Samolot" (flight vs. other transport) — no clock time. Flagged as unknown, not fetched further (would need a detail page) |
| URL | not embedded directly per offer in `__NEXT_DATA__`, but reconstructable | confirmed instead via JSON-LD `ItemList` (§6) |
| Tour operator | `tourOperator` / `tourOperatorName` | e.g. "Coral Travel" — extra, not a required field but free context |

## 5. Filter mechanism — structurally identified, legality of direct use NOT confirmed

The embedded search-query object (`queries[1].queryKey`) is the literal internal
API call (`method: "search.tripsSearch"`) that produced this page's data, and it
documents every filter dimension the backend supports:

```
query.departure           — airport filter (null in our fetch = all airports)
query.duration.min/max    — nights (was min:7, max:28 for this default page)
query.rooms[].adult/kid   — party composition (was already [{"adult":2,"kid":0}] — matches our default!)
query.maxPrice/minPrice   — price cap
query.minCategory/maxCategory — star range
query.service[]           — board codes (all six included by default: [1,2,3,4,5,6])
query.sort, query.order   — sort mechanism
query.pageNumber, listingOffers.limit — pagination (limit 10 per page in this sample)
```

This confirms the *shape* of filtering (airport, duration, price, stars, board,
sort all exist as backend parameters) but **does not confirm a legal way to
invoke it directly**: the call is `method: "search.tripsSearch"`, which strongly
resembles the kind of endpoint `Disallow: /ajax/` and `Disallow: /*.json` are
meant to cover, and most query-string/URL filter shapes Wakacje.pl uses for its
*rendered* filtered pages (comma segments, `?od-*`/`?do-*`, sort suffixes) are
explicitly disallowed by robots.txt (§1). Whether the same `/lastminute/` (or
`/wczasy/`) URL re-renders **its embedded `__NEXT_DATA__`** with different
filters when given a *robots-allowed* query string was not tested this session
(would require a live GET). **Open question — see §8b.3 and the concrete test
proposed in §10.** Separately (session 2, §11.3): combining more than one
filter into a single URL does appear to be **not** robots-legal — every
multi-filter link found on the page uses a comma-joined shape that
`Disallow: /*,*/ ` blocks (§8a.4) — so even if single-flag filtering works,
it looks likely to be one dimension at a time, not a combined query.

## 6. Variant semantics — one card is NOT one unambiguous variant

Across all 10 sampled offers, every record had **both**:
- `departurePlace` / `departurePlaceCode` — one specific airport, the one the
  shown `price` is actually for, and
- `departurePlaces` — a list of *other* airports (2 to 13 of them per offer in
  this sample) this same hotel/date/board combination can also depart from,
  **with no price given for any of them**.

Example (offer `208550`, Galeri Resort): shown price 6502 PLN is for departure
from **Katowice**; the offer is *also* biddable from Rzeszów, Zielona Góra,
Gdańsk, Wrocław, Bydgoszcz, **Łódź**, Lublin, Kraków, Poznań, Szczecin,
Warszawa-Radom and Warszawa — but at unknown, unconfirmed prices per airport.

**This is the same ambiguous-card problem Rainbow had.** A card is one
hotel/date/board/price combination for exactly one (already-shown) departure
airport — it must **not** be read as "available from all of `departurePlaces` at
this price." For our airport priority (LCJ first, then WAW/KTW/WRO), most
sampled cards were priced for a *different* airport than any of ours, even when
one of ours appeared in `departurePlaces`. No LCJ-priced card appeared in this
10-offer sample (LCJ appeared only inside `departurePlaces` lists, never as the
priced `departurePlace`).

Confirmed detail-page URL pattern (from JSON-LD `ItemList`, not guessed):
```
https://www.wakacje.pl/oferty/<country-slug>/<region-slug>/<city-slug>/<hotel-slug>-<offerId>.html
```
e.g. `https://www.wakacje.pl/oferty/turcja/wybrzeze-egejskie/didim/laur-experience-elegance-211281.html`.
This path shape is **not** blocked by robots.txt (no query string, no comma
segment) — a future bounded detail-page fetch to confirm one specific
airport/price combination (mirroring ITAKA's `itaka_details.py` /
`confirm_detail` pattern) looks legally viable, but was **not fetched this
session** (would be a second live GET).

## 7. Playwright verdict

**Not needed for the listing itself.** `__NEXT_DATA__` on the plain,
robots-allowed `/lastminute/` URL already contains full, structured offer data —
no client-side JS execution is required to read it, matching ITAKA's and TUI's
`category_ssr` precedent exactly.

**Open question for next session (not yet answered — do not assume either
way):** whether a robots-allowed *filtered* URL (if one exists — e.g. does
`/lastminute/` accept a plain, non-disallowed query string that narrows
`departure`/`duration`/`price`?) re-renders `__NEXT_DATA__` server-side with
those filters applied, the same way TUI's category route did filters via SSR
without Playwright. If no such legal filtered URL exists, the fallback is:
fetch the plain unfiltered listing (as today) and filter entirely client-side in
our own code — which is already exactly what `filtering.matches()` does for
every provider, so this would not be a blocker, just less efficient than a
provider that supports server-side filtering.

## 8. Unknowns

Updated after the session-2 offline pass (§11). Split into what got resolved (or
substantially clarified) purely from already-saved evidence, and what still
needs a live request.

### 8a. Resolved or substantially clarified offline (session 2, §11 has evidence)

1. **Price semantics — resolved with high confidence.** The page's own filter
   schema (§11.3) has a `priceType` toggle with exactly two options, `"Za
   wszystkich"` ("for everyone" / total) and `"Średnia za osobę"` ("average per
   person," slug `za-osobe`), and `input.defaultValue` points at the **total**
   option. This independently corroborates the embedded search query's own
   `"pricePerPerson": false, "totalPrice": true` flags found in session 1. Two
   independent pieces of the site's own embedded metadata agree: **`price` is
   the total for the queried room (2 adults / 0 kids in our default sample,
   matching our business default exactly)**, not a per-person figure. Still not
   verified against one real arithmetic example (no detail-page cross-check
   done), so treat as *resolved-by-strong-inference*, not lab-confirmed — but no
   longer a blind guess.
2. **Board code → board type mapping — resolved as reliable at the bucket
   level.** The site's own `cateringList` filter definition (§11.3) gives a
   complete, confirmed code table: `1`=`all-inclusive` (AI family), `2`=`HB`,
   `3`=`BB`, `4`=`wlasne` (no board), `5`=`ZO` (itinerary-based board, round
   trips), `6`=`FB`. The earlier "code 1 maps to both Ultra AI and AI" finding
   is now understood correctly: **the numeric `service` code is the reliable
   bucket** (exactly what a board mapping table needs), and `serviceDesc` free
   text is only a finer *label within* that bucket (Ultra AI vs. plain AI, both
   code `1`) — not a sign the code itself is ambiguous. A Wakacje-specific
   mapping (`1→AI, 2→HB, 6→FB`, matching `boards.py`'s existing per-provider
   pattern) can be built directly from this, no live confirmation required.
   FB/HB were still not seen in an actual *offer* record in this small sample,
   but the mapping comes from the site's own filter schema, not from inferring
   it across offer records.
3. **Price-range (`minPrice`/`maxPrice`) filtering — resolved as not legally
   invokable via URL.** The `priceRange` filter widget exists structurally, and
   its evident query-string form (`od-<min>`/`do-<max>`, matching the pattern
   visible in real detail-page hrefs, §11.4) is exactly what robots.txt's
   `/*?od-*`, `/*?do-*`, `/*,od-*`, `/*,do-*` rules disallow. No further live
   test needed to reach this conclusion.
4. **Multi-filter combination via one URL — resolved as not legally invokable.**
   Every real link on the page that combines more than one filter (the
   per-offer detail-page hrefs, §11.4) uses a comma-joined segment
   (`?od-...,7-dni,all-inclusive,z-katowic,srcx_v2_auction`), which is exactly
   the shape `Disallow: /*,*/ ` in robots.txt blocks wholesale. **Only
   single-flag URLs (`/lastminute/?<one-slug>`, no comma) are structurally
   robots-legal** — confirmed against the full ruleset, not yet against a live
   response (see §8b.3).
5. **`departurePlaces` structure — confirmed, not just observed.** It is a
   flat list of plain city-name strings only: no airport codes, no per-city
   offer IDs, no per-city price hint of any kind (§11.2). This reinforces §6's
   original finding and adds: the numeric `id`/`offerId` is **shared** across
   whichever departure airport happens to be the priced one for a given fetch —
   it is *not* by itself a full variant identifier. The real per-airport
   variant identifier only exists as the comma-joined, robots-disallowed detail
   URL query string (§11.4) — see the new, more precise open question in §8b.4.
6. **Discount fields exist but were never non-zero.** `priceDiscount` and
   `priceOld` are present on every offer record, and were `0` on all 10 sampled
   offers (§11.1) — the mechanism is real (the page also has active promo
   flags: `promoFirstMinute`, `promoLastMinute`, `promoTop10`), but no offer in
   this sample actually carried a discount, so the exact arithmetic relationship
   between `price`, `priceDiscount` and `priceOld` remains unverified by
   example (naming strongly implies `priceOld − priceDiscount == price`, but
   that is still inference, not evidence).
7. **New context, not a blocker:** the `providerList` filter (§11.3) shows
   Wakacje.pl is a **meta-aggregator** — its own filter list includes Itaka,
   Rainbow Tours and TUI as bookable tour operators alongside dozens of others.
   Overlap with our other three providers' own offers is plausible and already
   handled conservatively by `duplicate_key()` in `models.py` (falls back to
   per-provider grouping unless fields line up exactly) — worth knowing when
   interpreting alert volume later, not something to solve now.

### 8b. Still open — need a live request (§10 has the plan; not executed)

1. **Review count semantics** (`ratingReservationCount` / `ratingRecommends`).
   No new evidence found this pass; the filter schema's own rating widget
   (`ratingList`) is titled "Ocena klientów" (customer rating) but doesn't
   mention counts. Still genuinely unknown whether it means completed bookings
   or written reviews.
2. **Departure time.** Still absent from every place checked in the saved
   evidence (offer records, filter schema, SEO text). Only a detail-page fetch
   can resolve this.
3. **Whether a single-flag, robots-legal filter URL actually changes the
   server-rendered `__NEXT_DATA__`.** Structurally legal (§8a.4), several
   real candidate slugs are confirmed to exist as on-page links for our own
   target airports (`?z-wroclawia` — WRO is one of ours) and other dimensions
   (`?all-inclusive`), but none was fetched — it's possible these links are
   inert without further client-side JS, or that they do drive SSR the way
   TUI's `category_ssr` route did. Not assumed either way.
4. **Whether the *bare* (no-query) `/oferty/.../slug-id.html` detail page
   renders the same priced variant shown on the listing card.** This is a
   sharper version of session 1's open item: we now know the *real* per-card
   link needs a robots-disallowed comma-joined query string
   (`?od-...,7-dni,all-inclusive,z-katowic,srcx_v2_auction`) to pin the exact
   date/duration/board/airport combination. Stripping that query to stay
   robots-legal might land on a different default variant (e.g. cheapest
   airport, not the one shown), or might coincidentally match — genuinely
   unknown, not guessed either way.
5. **Whether `/oferty/...` detail pages carry mandatory-cost / booking-total
   confirmation data**, needed for `price_is_complete=True` — not checked.
6. **LCJ's own single-flag departure slug.** Confirmed slugs exist for
   Gdańsk (`z-gdanska`), Poznań (`z-poznania`), Wrocław (`z-wroclawia`, one of
   our own targets), and — only inside the robots-disallowed comma-joined
   detail hrefs — Katowice (`z-katowic`), Kraków (`z-krakowa`), Rzeszów
   (`z-rzeszowa`), Warszawa-Radom (`z-warszawy-radom`). **No slug for Łódź
   (LCJ) or plain Warszawa (WAW, as opposed to Warszawa-Radom) was found
   anywhere in the saved evidence.** Do not guess `z-lodzi` — it follows the
   visible pattern but is unconfirmed, and "Warszawa" vs. "Warszawa - Radom"
   are evidently two distinct departure points on this site, so guessing wrong
   is a real risk here, not a formality.
7. **Whether combining two single-flag filters with `&` (not a comma) works
   and stays robots-legal.** No example of this shape was found anywhere in
   the saved HTML, either confirming or ruling it out.
8. **Discount field arithmetic** (§8a.6) — needs a real non-zero example,
   likely only obtainable from a detail page or a differently-parameterized
   listing, neither fetched this session.

## 9. Recommended architecture — listing-only HTTP (revised after §12; superseded the original detail-enrichment plan)

Same HTTP-only, budgeted, robots-checked shape as `ItakaProvider`/`itaka_rsc.py`
(`travel_deal_agent/providers/http.py`'s `RequestBudget`/`UrllibTransport`,
`itaka.py`'s `robots_policy`) — **no Playwright**, and, after §12.2's finding,
**no detail-page enrichment stage either** (dropped — see §12.4 for why):

- `wakacje_query.py` — build the small set of robots-legal, single-flag listing
  URLs to fetch: the unfiltered `/lastminute/` baseline, plus one
  `/lastminute/?z-<city>` request per *confirmed* target-airport slug (WRO via
  `z-wroclawia`, plain Warszawa via `z-warszawy`, Katowice via `z-katowic` —
  all confirmed real slugs, §11.4/§12.1; **LCJ still has no confirmed slug**,
  §8b.6 — needs an explicit decision, not a guess, before it can be included
  here). Never combine this with any other filter in the same URL (§8a.4).
- `wakacje_data.py` — parse `__NEXT_DATA__`, normalize `offers.data` records
  into `Offer`, preserving the native 0–10 rating scale, dividing the confirmed
  total `price` by `number_of_people` for `price_per_person` (§8a.1), and
  mapping `service` codes to AI/HB/FB via the confirmed table
  `1→AI, 2→HB, 6→FB` (§8a.2, §11.3) — no per-provider ambiguity left to resolve
  here, unlike TUI's original board-alias gap. Each offer's `variant_identity`
  should incorporate the search context it came from (at minimum the
  departure-airport filter used), not just the raw numeric `id` — §12.1 showed
  the same `id` can carry a different date/price under a different departure
  filter.
- `wakacje.py` (`Provider` implementation) — orchestrates the above,
  `price_is_complete=False` **unconditionally** (§12.4 — not a temporary gap
  pending a future detail-page fetch, but the current ceiling given no
  robots-compliant, non-Playwright path to a booking-total exists), `disabled`
  by default in `config.json` exactly like ITAKA/Rainbow/TUI, using the same
  `RequestBudget` bounded-HTTP pattern as ITAKA.

No `wakacje_details.py` module. This makes Wakacje.pl the simplest of the four
providers architecturally (fewer moving parts than ITAKA's detail-confirmation
flow or Rainbow/TUI's browser-capture flow), at the cost of never being able to
reach `price_is_complete=True` by itself — same ceiling Rainbow and TUI already
have, documented in `CURRENT_STATE.md`'s roadmap as a shared future item, not
specific to this provider.

## 10. Two-GET plan — executed in session 3 (§12 has the results)

The plan below was proposed in session 2 and **explicitly authorized and
executed in session 3** — kept here as a record of what was asked and why;
results and their resolution of §8b are in §12.

**GET 1 — filtered listing (executed):** `https://www.wakacje.pl/lastminute/?z-wroclawia`.
Goal: resolve §8b.3 (does a robots-legal single-flag filter actually change
server-rendered `__NEXT_DATA__`?). **Result: yes, confirmed — §12.1.**

**GET 2 — one detail page (executed):**
`https://www.wakacje.pl/oferty/turcja/wybrzeze-egejskie/didim/laur-experience-elegance-211281.html`.
Goal: resolve §8b.1/§8b.2/§8b.4/§8b.5/§8b.8. **Result: the page carries no
offer-specific data at all in its robots-legal form — §12.2; this resolves
§8b.4 and rules out a detail-page path for §8b.2/§8b.5 (still open, but now
known to be unreachable this way — see §8b below).**

Both stayed within robots.txt (§1); exactly these two requests were made, no
more. `wakacje.py` is still not implemented — see §12.4/§9 for the (revised)
architecture recommendation before writing it.

### Updated §8b status after session 3

- §8b.3 (single-flag filter legality/effect) — **resolved: yes** (§12.1).
- §8b.4 (does bare detail page preserve the variant) — **resolved: no, it
  shows no offer data at all** (§12.2), a more decisive answer than either
  original possibility ("same variant" / "different default variant").
- §8b.1 (review-count semantics), §8b.2 (departure time), §8b.5
  (mandatory-cost data) — **still open**, and now known to be **unreachable
  via any robots-legal detail-page fetch** (§12.2/§12.3). Resolving them would
  require either a different, not-yet-identified legal page/endpoint, or
  client-side JS execution (Playwright) — a separate decision, not something
  to pursue by guessing at more URLs.
- §8b.6 (LCJ's departure slug) — **still unresolved**; also checked (for free,
  no extra request) against the Wrocław-filtered page's own on-page links in
  §12.1 — still absent everywhere.
- §8b.7 (`&`-joined multi-filter legality) — **still open**, not tested this
  session (out of the authorized two-GET budget).
- §8b.8 (discount-field arithmetic) — **still open**; the one offer fetched in
  GET 2 carried no visible price data at all, so no example was obtained.

## 11. Session 2 — deeper offline analysis (2026-09-22, same saved evidence)

No network request made this pass. Everything below comes from re-reading
`next_data.json` and `lastminute.html` (already on disk from session 1) more
thoroughly.

### 11.1 Exact price-field structure (all offer-level price/discount keys)

Full set of price-adjacent keys on an offer record (union across all 10
sampled offers, nothing filtered out): `price`, `priceDiscount`, `priceOld`,
`originalCurrency`, `shownCurrency`, plus the promo-flag fields `promoFirstMinute`,
`promoLastMinute`, `promoTop10`, `promotionTags` (empty list in every sample).
**No per-offer field for number of people** exists on the offer record itself
(no `adults`/`pax`/`rooms` echoed back) — the room composition is only known
from the *request* that produced the page (§3/§5), not from each offer. Across
all 10 offers, `priceDiscount` and `priceOld` were `0` in every case — see
§8a.6.

### 11.2 `departurePlace` / `departurePlaces` — exact shapes

```
"departurePlace": "Katowice",       # str — the ONE airport the shown `price` is for
"departurePlaceCode": "KTW",        # str — IATA-style code for that same one airport
"departurePlaces": [                # list[str] — city NAMES only, no codes, no prices, no IDs
  "Rzeszów", "Zielona Góra", "Gdańsk", "Wrocław", "Bydgoszcz",
  "Katowice", "Łódź", "Lublin", "Kraków", "Poznań", "Szczecin",
  "Warszawa - Radom", "Warszawa"
]
```
Confirms §6's original finding at the field-type level: there is no hidden
per-city price or code inside `departurePlaces` to exploit — it is genuinely
just an informational list of *other* possible departure cities for that same
hotel/date/board, unpriced. `id`/`offerId` do not vary with the departure
airport (same numeric ID regardless of which airport happens to be shown), so
they cannot serve alone as a variant identifier once more than one airport is
in play — see §8b.4.

### 11.3 Full sidebar filter catalog (`props.stores.sidebarFiltersStore.sidebarFiltersData`)

The page embeds its own complete filter-widget configuration — 13 widgets, each
with machine-readable `slug`s for every option. This is the single most useful
find of this pass: it answers "what are the exact filter parameters" (task
item 9) directly from the site's own metadata, with no guessing.

| Filter (`type`) | Title | Confirmed slugs relevant to us |
|---|---|---|
| `order` | Sortuj od | `tanio` (cheapest first), `ocena-malejaco` (rating desc); default "Najpopularniejszych" has no slug (no query param needed) |
| `cateringList` | Wyżywienie | `all-inclusive`(code 1, AI family), `HB`(code 2), `BB`(code 3), `wlasne`(code 4, no board), `ZO`(code 5, itinerary-based), `FB`(code 6) |
| `durationList` | Długość pobytu | `<n>-dni` for n=2..28 (`1-dzień` for exactly 1 night) — **`7-dni`, `8-dni`, `9-dni` are our range** |
| `objectCategoryList` | Standard hotelu | `3-gwiazdkowe`, `4-gwiazdkowe`, `5-gwiazdkowe`, `2-gwiazdkowe` — single-select, "at least N stars" |
| `ratingList` | Ocena klientów | `ocena-9`, `ocena-8`, `ocena-7`, `ocena-6` — "from X.0" on the native 0–10 scale |
| `priceType` | Cena (toggle) | `za-osobe` (per-person view); the total-view option has no slug shown and is the confirmed default (§8a.1) |
| `priceRange` | (none) | numeric min/max, no slug — its evident URL form matches robots.txt's disallowed `od-`/`do-` pattern (§8a.3) |
| `facilityList` | Udogodnienia | e.g. `bez-paszportu` (matches robots.txt's own explicit `Allow:` carve-out under `/wczasy/`), `przy-plazy`, `z-basenem`, etc. — not needed for our filters |
| `hotelTypeList` | Rodzaj hotelu | `dla-dzieci`, `hotele-dla-doroslych` — not needed |
| `providerList` | Biuro podróży | tour-operator filter; list includes `itaka`, `rainbow-tours`, `tui` among 30+ others — confirms Wakacje.pl is a meta-aggregator (§8a.7) |
| `transportType` | Sposób dojazdu | `samolotem` (flight) — could restrict to flights only if ever needed |
| `offerType` | Rodzaj podróży | `wypoczynek` (leisure) etc. — not needed |
| `promotionList` | Typ oferty | `lastminute`, `firstminute` — not needed, already on the last-minute category page |

**No airport/departure filter widget exists in this catalog at all.** Airport
filtering is evidently driven by a separate mechanism (not this sidebar-filter
schema) — see §11.4 for what was found about it instead.

### 11.4 Real on-page links: confirmed single-flag URLs and the comma-joined variant-URL shape

Two distinct URL shapes were found by scanning every `href` in the raw HTML —
neither guessed, both literal on-page links:

**A. Single-flag filters on `/lastminute/`** (no comma, no query beyond one
flag) — structurally robots-legal (§8a.4):
```
https://www.wakacje.pl/lastminute/?all-inclusive
https://www.wakacje.pl/lastminute/?najblizszy-tydzien
https://www.wakacje.pl/lastminute/?z-gdanska
https://www.wakacje.pl/lastminute/?z-poznania
https://www.wakacje.pl/lastminute/?z-wroclawia      <- one of OUR target airports (WRO)
```
Plus country-scoped plain listings (no query at all, also robots-legal):
`/lastminute/egipt/`, `/lastminute/turcja/`, `/lastminute/grecja/`,
`/lastminute/bulgaria/`, `/lastminute/tunezja/`, `/lastminute/hiszpania/`.

The SEO FAQ text (§4/§11) also *names* Katowice, Warszawa, Kraków, Szczecin and
Łódź as other Last-Minute departure cities, but only Gdańsk/Poznań/Wrocław are
given as actual `href`s there — the others' exact slugs are not confirmed by
that text alone (see next point for partial confirmation of three more).

**B. The real per-offer detail-page link — comma-joined, robots-disallowed:**
```
https://www.wakacje.pl/oferty/turcja/riwiera-turecka/okurcalar/galeri-resort-208550.html?od-2026-10-22,7-dni,all-inclusive,z-katowic,srcx_v2_auction
```
(nine such links found, one per sampled offer). This is the exact variant
identifier — date, duration, board and priced departure airport all in one
string — but the query string is precisely what `Disallow: /oferty/*?*`
forbids. It does incidentally confirm three more departure-airport slugs not
seen elsewhere: `z-katowic` (Katowice/KTW), `z-krakowa` (Kraków/KRK),
`z-rzeszowa` (Rzeszów/RZE), `z-warszawy-radom` (Warszawa-Radom). **Łódź (LCJ)'s
slug still does not appear anywhere** — see §8b.6.

The comma-joined shape (`od-...,7-dni,all-inclusive,z-katowic,srcx_v2_auction`)
is also, evidently, how the sidebar's own multi-filter selections would combine
into one URL — and it is exactly the shape `/*,*/ ` in robots.txt blocks
wholesale (§8a.4). No `&`-joined alternative was found anywhere in the saved
HTML (§8b.7) — genuinely unknown, not ruled out.

### 11.5 UTF-8 re-confirmation

Re-verified at the byte level again this pass (`Wybrzeże` → UTF-8 bytes
`c5 bc` for `ż`) — no change from session 1, just re-confirmed while digging
through more of the same file; mangled diacritics remain a terminal-display
artifact only, never a data issue.

## 12. Session 3 — the two authorized live GETs (2026-09-22)

Raw evidence added to `data/wakacje-recon/` (gitignored): `headers-wroclawia.txt`,
`lastminute-z-wroclawia.html` (852 KB) + its extracted `..._next_data.json`;
`headers-detail.txt`, `detail-laur-211281.html` (358 KB) + its extracted
`..._next_data.json`. Both requests: `200 OK`, nginx, no CAPTCHA, no rate-limit
signal. Exactly two requests made, nothing else.

### 12.1 GET #1 — `?z-wroclawia` decisively changes server-rendered data

Comparing the filtered response's embedded search request against session 1's
baseline: **`query.departure` changed from `null` to `[256]`** (Wrocław's
internal numeric city ID) — everything else in the query (`duration`, `rooms`,
`service`, `sort`, `minPrice`/`maxPrice`, `pricePerPerson`/`totalPrice`) stayed
at the exact same defaults, confirming this was a clean, isolated single-filter
change, not an accidental combination.

The result: **all 10 returned offers now have `departurePlace: "Wrocław"`,
`departurePlaceCode: "WRO"`**, and the total match count dropped from 23,792
(unfiltered) to 11,539 (Wrocław only). This is conclusive, direct proof —
**`?z-wroclawia` is not decorative; it drives real server-side filtering of
`__NEXT_DATA__`.** This resolves §8b.3 as a firm **yes**.

**A sharper, more important finding sits underneath this**, found by diffing
offer `211281` (Laur Experience & Elegance) between the two listings — same
`id`, fetched under two different departure-filter contexts:

| Field | Unfiltered listing (session 1) | `?z-wroclawia` listing |
|---|---|---|
| `departurePlace` / code | Rzeszów / RZE | **Wrocław / WRO** |
| `departureDate` → `returnDate` | 2026-10-13 → 2026-10-20 | **2026-10-22 → 2026-10-29** |
| `price` | 5559 PLN | **5938 PLN** |
| `departurePlaces` | 7 cities (incl. Wrocław) | **`["Wrocław"]` only** |
| `keyHash` (booking token) | one opaque token | **a different opaque token** |
| `service`/`serviceDesc`, `category`, `ratingValue`, `ratingReservationCount`, `offerHash`, `tourOperator` | identical | identical |

**The same numeric `id` resolved to a different departure date, not just a
different price, once the departure airport changed.** All 10 offers in the
Wrocław-filtered response show `departurePlaces: ["<that one city>"]` only —
confirming `departurePlaces` is **contextual to the specific search that
produced the response**, not a fixed hotel-level property: the backend
evidently picks whichever date/flight instance best fits the requested
departure city (within the broader duration window), and that instance's own
available-departure-city list is what gets echoed back. This is a materially
sharper version of §6's original finding: **a numeric `id` alone, even paired
with one `departurePlace`, is not a stable variant identifier across different
fetches — the variant is only fully pinned by (id + the exact search context
that produced it: departure filter, date window, etc.).** This still doesn't
change the core rule (never read `departurePlaces` as "available at the shown
price") — it strengthens it.

Bonus, incidental find: two more real departure-airport slugs appeared as
on-page links on this filtered page: **`z-warszawy`** (plain Warszawa, distinct
from the already-known `z-warszawy-radom`) and a repeat of `z-katowic`. Still
**no `z-lodzi` (or any Łódź slug) anywhere** — checked this page too, not just
session 1's — see updated §8b.6.

### 12.2 GET #2 — the bare detail page carries no offer-specific data at all

This is the decisive, somewhat unexpected result. The detail page **did render
correctly** (`ssr: true`, correct `<title>`: "Wakacje w Laur Experience &
Elegance w Turcji z Coral Travel", correct breadcrumb JSON-LD down to the hotel
name) — it is not an error or a block. But its `__NEXT_DATA__` has only **two**
dehydrated queries: `header-content` (site chrome) and `legalInformations`
(generic legal boilerplate about the travel-agent/tour-operator relationship,
required by consumer-protection law — not offer-specific). **No price, no
dates, no board, no departure airport, no rating count, no offer ID appear
anywhere in the page's `__NEXT_DATA__`.**

Checked directly in the rendered HTML body too, not just the JSON: the string
`"PLN"` appears exactly **once** in 357,877 bytes (in unrelated boilerplate
text, not a price figure), no price-shaped digit patterns were found, and a
`skeleton`-class CSS hook was found — consistent with the real offer/price
widget being loaded by **client-side JavaScript after initial render** (i.e.
an API call this recon correctly did not make, and which — being under
`/ajax/`/`*.json` in spirit — would likely be robots-disallowed to call
directly even if its exact URL were known).

The page does have a real, genuine reviews section (`id="opinie"`) with
review-verification copy ("Weryfikacja opinii" — reviews checked against the
email address of an actual reservation) — useful context for §8b.1 (it
confirms reviews on this site are reservation-linked/verified) but still does
**not** literally state what `ratingReservationCount` counts — no visible
review count number was found in the legally-fetched content.

**Conclusion: the bare, robots-legal detail-page path cannot confirm any
variant at all** — not "a different variant than the listing," but **none**.
This resolves §8b.4 conclusively, in the more consequential direction: an
ITAKA-style bounded detail-page confirmation step is **not achievable** for
Wakacje.pl without either (a) using the robots-disallowed query string, which
is out of scope, or (b) executing client-side JS (Playwright), which is not
authorized and not what this recon was scoped to test.

### 12.3 Fields checked against the original request list (GET #2 specifically)

| Requested field | Found on bare detail page? |
|---|---|
| Lotnisko (airport) | No |
| Godzina wylotu (departure time) | No — only generic legal boilerplate mentioning the operator's *duty* to inform about departure time/place, not actual data |
| Termin (dates) | No |
| Długość pobytu (duration) | No |
| Wyżywienie (board) | No |
| Cena (price) | No |
| Pełna cena / opłaty obowiązkowe | No |
| Rating | No |
| Liczba opinii/rezerwacji | No (only a generic, non-numeric reviews *section*) |
| Identyfikator wariantu/oferty | No (the numeric ID does appear in the URL itself, but nothing in the page content ties back to it) |
| Dane touroperatora | Partially — "Coral Travel" appears in the page `<title>`, but not as structured data |

### 12.4 Why this changes the recommended architecture (§9, rewritten below)

Given §12.2, `wakacje_details.py` (the previously proposed ITAKA-style bounded
detail-page confirmation module) is **dropped** — there is nothing legal for it
to fetch that would add information. In its place, §12.1 supplies a better,
fully robots-legal alternative for the exact problem detail-page confirmation
was meant to solve (pinning one departure airport unambiguously): **fetch one
single-flag `?z-<city>` listing per target airport.** Each such fetch returns
offers where `departurePlace`/`departurePlaceCode` match that airport exactly
and `departurePlaces` collapses to that one city — i.e. **already
unambiguous, airport-specific pricing, with no detail-page step needed at
all.** This is simpler than every other provider's variant-confirmation
story (ITAKA's detail fetch, Rainbow's passive-capture join, TUI's passive
XHR capture) — Wakacje.pl's ambiguity is resolved by picking the right
*listing* request, not by a second confirmation stage.

Trade-offs of this approach, to carry into the next planning session:
- Each single-flag listing page returns only the top `limit=10` results under
  the default "Najpopularniejszych" (most popular) sort — not price-sorted,
  not pre-filtered by our ≤1500 PLN/person cap, duration, or stars (robots.txt
  disallows combining a price/sort filter with the airport filter in one URL,
  §8a.4). All of our business filters still apply purely client-side via the
  existing shared `filtering.matches()`, same as every other provider — but
  there's a real chance the top-10-by-popularity page for a given airport
  rarely contains anything under a strict PLN 1500/person cap, which may argue
  for either accepting sparse/zero results (like Rainbow's own live outcome)
  or, in a future session, separately testing the `tanio` (cheapest-first)
  sort slug — also a single, non-comma flag, so structurally the same
  robots-legal shape, but **not tested this session and not to be combined
  with the airport filter without checking that combination's legality
  first**.
- LCJ (our top-priority airport) still has no confirmed filter slug (§8b.6,
  now checked across three separately-fetched pages with the same negative
  result) — the provider's first version may need to either accept fetching
  unfiltered results and relying on `departurePlaceCode == "LCJ"` matches
  turning up by chance (unreliable, given LCJ never appeared as a priced
  `departurePlace` in any sample so far), or treat this as a specific
  outstanding item before LCJ coverage can be trusted.
- `price_is_complete` should stay `False` unconditionally, same as Rainbow and
  TUI — not because it's unverified-but-maybe-fixable-later via a detail page
  (as ITAKA's design assumes), but because §12.2 shows there is currently no
  robots-compliant, non-Playwright path to ever confirm a Wakacje.pl booking
  total. This is a firmer, more permanent version of the same open item every
  other provider already carries (see `CURRENT_STATE.md` §5 roadmap item).

## Summary (updated after session 3 — the two authorized live GETs)

1. **Did `?z-wroclawia` actually change SSR/`__NEXT_DATA__`?** Yes, confirmed
   directly: `query.departure` changed from `null` to `[256]`, all 10 returned
   offers switched to `departurePlace: "Wrocław"` / `WRO`, and the total match
   count changed (23,792 → 11,539). Not decorative — real server-side
   filtering (§12.1).
2. **Can a specific Wrocław variant be confirmed?** Yes, at the listing level —
   each of the 10 offers returned is already unambiguously priced for Wrocław,
   with `departurePlaces` collapsed to `["Wrocław"]` only. But confirmed with
   an important caveat: the *same numeric `id`* that showed Rzeszów/2026-10-13/
   5559 PLN in the unfiltered listing showed Wrocław/2026-10-22/5938 PLN here —
   a different date, not just a different price. The variant is only pinned by
   **(id + the exact search context)**, never by `id` alone (§12.1).
3. **Did the detail page confirm the same variant as the listing?** No — it
   confirmed **no variant at all**. The bare, robots-legal detail-page path
   carries zero offer-specific data (no price, dates, board, airport, or
   rating count) anywhere in its `__NEXT_DATA__` or rendered HTML — only site
   chrome and generic legal boilerplate (§12.2).
4. **Were flight times found?** No — not in the filtered listing (same field
   set as before, no time field) and not on the detail page (only generic
   legal text about the operator's *duty* to disclose departure time/place,
   not actual data) (§12.2, §12.3).
5. **Were full price / mandatory costs found?** No — the detail page had no
   price data of any kind in its legally-fetchable content (§12.2, §12.3).
6. **What does `ratingReservationCount` mean, exactly?** Still not literally
   confirmed. New context only: the detail page has a genuine,
   reservation-verified reviews section ("Weryfikacja opinii" — reviews
   checked against the email of an actual booking), which is consistent with
   (but does not prove) `ratingReservationCount` meaning verified,
   booking-linked reviews rather than raw completed-booking counts. Treat as
   still open (§8b.1, §12.2).
7. **Is one detail-page fetch enough to confirm a variant?** No — decisively
   no. It's not that one fetch is insufficient; the robots-legal form of that
   page contains no offer data to confirm anything with (§12.2, §12.4).
8. **Is Wakacje.pl ready for provider implementation after these two GETs?**
   Ready for a **listing-only** provider, on the same footing Rainbow and TUI
   already operate on: real offers, real fields, `price_is_complete=False`
   always. Not "ready pending one more check" — the detail-confirmation path
   this recon was checking for turned out not to exist within robots.txt, so
   that's a settled architectural fact, not a remaining unknown. Two smaller
   items (LCJ's filter slug, §8b.6; whether the top-10-per-airport results
   ever intersect our strict PLN 1500/person cap, §12.4) are implementation
   details to resolve while building, not additional recon blockers.
9. **Recommended architecture: pure HTTP or HTTP + bounded detail
   enrichment?** **Pure HTTP, listing-only** (§9, revised). No Playwright
   (confirmed unnecessary for the listing, §3/§7), and no detail-enrichment
   module (confirmed non-viable within robots.txt, §12.2/§12.4) — the
   ambiguous-departure-airport problem that a detail step would normally solve
   is instead solved by fetching one single-flag `?z-<city>` listing per
   target airport (§12.1), which is simpler than every other provider's
   variant-confirmation mechanism (ITAKA's detail fetch, Rainbow's
   passive-capture join, TUI's passive XHR capture).

**Still open, not resolved by any of the three sessions, and not reachable via
a robots-legal HTTP request found so far:** review-count exact semantics
(§8b.1), departure time (§8b.2), mandatory-cost/booking-total data (§8b.5),
LCJ's departure-filter slug (§8b.6), `&`-joined multi-filter legality (§8b.7),
discount-field arithmetic with a real non-zero example (§8b.8). None of these
block starting a listing-only, `price_is_complete=False` provider — they
matter for the later, cross-provider "price completeness" roadmap item
(`CURRENT_STATE.md` §5), not for this provider's first version.

Per this session's scope, `wakacje.py` is **still not implemented** — the next
session should design and write it against §9's revised architecture.

## 13. Session 4 (same day, 2026-09-22) — `wakacje.py` implemented, then a business
problem surfaced: the project owner's own manual search on Wakacje.pl found real,
matching offers the shipped provider did not see. Six further controlled sessions
(13-20 below) diagnosed and partially fixed this. Full narrative and current status:
see `CURRENT_STATE.md` in this same directory, which is the authoritative,
up-to-date source of truth. This file remains the detailed evidence log; entries
below are terser than §1-12 since `CURRENT_STATE.md` carries the summary.

### 13a. `/wczasy/` vs `/lastminute/` — general search confirmed broader

Live GETs: `/lastminute/` (baseline re-verify) and `/wczasy/` (candidate for the
site's own "Dowolny kierunek lub hotel" general search). Both legal per a
freshly re-fetched robots.txt (byte-identical to §1-12's copy; still no
Crawl-delay).

- `/wczasy/`: `searchType: "wczasy"`, `query.type: []`, `query.service: []`
  (no last-minute-only or board restriction) — confirms this is the general
  search, not a last-minute variant.
- Match counts at fetch time: `/lastminute/` 23,577 vs `/wczasy/` 38,216 —
  materially larger and differently composed.
- Search-box value on `/wczasy/`, read directly from the rendered HTML:
  `"Dowolny kierunek lub hotel"` — the exact business term used throughout
  this project's requirements.

**Decision made this session:** `wakacje.py`'s `LISTING_PATH` was changed from
`/lastminute/` to `/wczasy/` as the baseline (implemented in a later session,
§15).

### 13b. Pagination confirmed real and robots-legal

On-page links found on both `/lastminute/` and `/wczasy/`: `?str-2` .. `?str-5`,
`?str-100` (single flag, no comma). A live confirmatory `GET /wczasy/?str-2`
returned different offer IDs than page 1, with `pageNumber: 2` embedded in the
SSR search-request object — genuine server-side pagination, not decorative.
No robots.txt rule matches `str-`.

**Combined with an airport filter:** `?str-2,z-wroclawia` is *also* a real,
site-generated link (found on the `?z-wroclawia` page's own pagination
widget), confirmed live: `pageNumber: 2`, `departure: [256]` (Wrocław), 9/10
offers genuinely new vs. page 1. `robots_policy`'s `/*,*/ ` rule does not match
this shape (it requires a `/` *after* the comma, which `str-2,z-wroclawia`
never has) — confirmed by direct reasoning through `robots_policy`'s regex
translation, not assumed.

**Combined with sort:** `?str-2,tanio` is the same real, site-generated shape
for the cheapest-sort page. Confirmed live: `pageNumber: 2, sort: 1, order: 0`,
10 new offers, but — as in the unfiltered `?tanio` sample — **all 10 still had
an empty `departurePlaceCode`** (no-flight/self-transport products).

**Never found, never guessed, never fetched:** `?tanio,z-wroclawia` (sort +
airport combined in one request). No on-page evidence of this shape exists
anywhere. Per this project's convention, it was not tested.

### 13c. `/wczasy/?tanio` — cheapest-first sort is real but useless for this business

`GET /wczasy/?tanio`: `200`, `query.sort: 1, query.order: 0` (vs. the default
`13/1`), count essentially unchanged (38,215). The 10 cheapest offers site-wide
were self-transport/no-flight products (Czarnogóra, Włochy, Grecja, Bułgaria,
Turcja, Sri Lanka, Polska) at 431-529 PLN/person — **none had a
`departurePlaceCode`**, so none would even pass `RawOffer`'s airport-code
regex validation. Same result on page 2 (§13b). **Decision:** `?tanio` is
never queried by the provider — confirmed dead end for our airport-based
business rules, not a coverage gap.

### 13d. KTW — `z-katowic` provenance, precisely

`z-katowic` text was searched for across every saved evidence file
(`/lastminute/`, `/wczasy/`, `/wczasy/?tanio`, `/wczasy/?z-wroclawia`,
`/wczasy/?str-2`). It appears **only** inside the comma-joined, robots-*disallowed*
per-offer detail hrefs (`/oferty/.../hotel-id.html?od-...,7-dni,all-inclusive,
z-katowic,srcx_auction` — blocked outright by `Disallow: /oferty/*?*`
regardless of the comma question). It was **never** found as a standalone
`href="...?z-katowic"` quick-filter link anywhere, unlike `z-wroclawia`.
**Conclusion at the time:** the string is real and almost certainly correct,
but not confirmed as a *standalone* filter — left unconfirmed and unused.
(Superseded in part by §20 below, which found it via the UI's own general
search — still not live-verified standalone as of this file's last update;
see `CURRENT_STATE.md` §2/§5 for the exact next step.)

### 13e. RDO identified, not added

`id=956826` ("Prestige Alanya") appeared in an unfiltered `/lastminute/`
baseline with `departurePlaceCode: "RDO"` — a real, distinct, priced airport
code (Warszawa-Radom), never seen before in this reconnaissance. Not one of
our configured airports (`filters.airports` lists WAW/WMI, not RDO) — noted,
not added.

## 14. Implementation session A (same day) — `price_is_complete` whitelist, `/wczasy/`, pagination

Full design discussion and rationale: see the conversation this file's project
tracks (not reproduced here). Summary of what shipped — current, authoritative
state is `CURRENT_STATE.md` §1:

- `filters["accept_incomplete_price_from"]` (new `FilterConfig` field): a
  per-provider whitelist letting `filtering.matches()` accept
  `price_is_complete=False` for a named provider. `config.json` sets
  `["wakacje.pl"]`. ITAKA/Rainbow/TUI/mock unaffected (empty/absent by
  default -> original unconditional requirement, pinned by regression tests).
- `storage.Store.observe()`'s alert gate simplified from
  `eligible and offer.price_is_complete` to `eligible` alone — `matches()` is
  now the single source of truth for "is this price usable," not duplicated
  in storage. Documented as a deliberate contract in the method's docstring,
  with an explicit regression test (`tests/test_pipeline.py`) proving
  `filter_batch` never passes `eligible=True` except when `matches()` agrees.
- `notification_content.NotificationMessage.render()` no longer raises for an
  incomplete price; it appends `"Price is from the listing — not yet confirmed
  at checkout/booking."` instead.
- `wakacje.py`: `LISTING_PATH` -> `/wczasy/`; `fetch()` paginates each
  confirmed airport up to `cfg.get("max_pages", 2)`, using the confirmed
  `?<slug>` (page 1) / `?str-<n>,<slug>` (page 2+) shapes from §13b, with a
  graceful budget-exhaustion break (mirrors `itaka.py`'s pattern) instead of
  raising mid-cycle.
- `price_is_complete` itself: **untouched**. Still unconditionally `False` for
  every Wakacje.pl offer, for the same structural reason as §12.2/§12.4.

## 15. Live test of the reimplemented provider (5 requests, WRO only at the time)

`robots.txt`, `/wczasy/`, `/wczasy/?z-wroclawia`, `/wczasy/?str-2,z-wroclawia`,
`/wczasy/?str-3,z-wroclawia` — all `200`. 25 raw records, 22 unique after
dedup, all `price_is_complete=False` as expected, airport distribution
`{WRO:21, KRK:1, KTW:1, POZ:1, RDO:1}` (the non-WRO ones by chance, from the
unfiltered baseline). Price range 1412-3827.5 PLN/person. **1** offer
<=1500 PLN/person found; **0** passed `matches()` — rejected on unmapped
country + (incidentally) sub-threshold rating. This result, on its own, is
what motivated the manual UI comparison in §16.

## 16. Manual UI comparison session — real offers the agent was missing

Full walkthrough (screenshots) lives in the conversation this file's project
tracks; findings reproduced here as durable evidence.

**Search flow used:** homepage -> "Jak i skąd?" modal -> departure-city
selector -> "Cena" (Średnia za osobę, do 1500 zł) -> "Wyżywienie"
(AI/HB/FB) -> "Standard hotelu" (3★+) -> "Ocena klientów" (od 8.0).

**Łódź slug, confirmed via the UI (not guessed):** selecting only Łódź in
"Jak i skąd?" and searching produced
`https://www.wakacje.pl/wczasy/?od-2026-09-22,samolotem,z-lodzi&src=fromSearch`
-> slug **`z-lodzi`**. Reproduced identically in a later session (§20). Only
ever seen inside a multi-filter, site-generated URL — never live-verified as
a standalone single-flag request (open item, see `CURRENT_STATE.md` §5).

**Warszawa sub-selector discovered:** the "Jak i skąd?" modal's "Warszawa"
entry has a "Pokaż lotniska" expander revealing two independent checkboxes:
**"Warszawa - Chopin"** and **"Warszawa - Modlin"** (both checked by default
under the parent). Selecting the parent alone (both sub-boxes stay checked)
produces `z-warszawy` in the URL; unchecking Modlin and keeping only Chopin
changes the "Twój wybór" chip to "Warszawa - Chopin" and produces
**`z-warszawa-chopin`** in the URL, with the result count dropping from 6 to
5 offers (a real, non-cosmetic effect). "Warszawa - Radom" is a separate,
non-nested list entry, not tested this session.

**Real offers found and recorded (business regression examples — see
`CURRENT_STATE.md` §3 for the canonical, structured version):**

1. **HTop Olympic (Calella), Hiszpania / Costa Brava**, departing **Wrocław**,
   21.10.2026-28.10.2026 (7 nights), HB, 4★ (page text also showed "Kategoria
   lokalna 3★" — a discrepancy, noted not resolved), rating 7.2 "Dobry" (265
   opinii), cena razem 2979 PLN -> **1489.50 PLN/os.** URL:
   `https://www.wakacje.pl/oferty/hiszpania/costa-brava/calella/htop-olympic-calella-700293.html?od-2026-10-21,7-dni,HB,z-wroclawia,srcx_v2_auction`.
   Found via: general search, Łódź+Katowice+Warszawa+Wrocław, <=1500 PLN/os.,
   AI/HB/FB.
2. **Alion, Albania / Riwiera Albańska / Durrës**, variant 1: departing
   **Katowice**, 03.10.2026-10.10.2026, HB, 4★, rating 8.6 "Bardzo dobry" (12
   opinii), od 1497 PLN/os. URL:
   `.../alion-1183473.html?od-2026-10-03,7-dni,HB,z-katowic,srcx_v2_auction` —
   **this exact variant showed "Oferta w tej konfiguracji jest niedostępna"**
   when clicked into (real-time inventory turnover / the same ambiguous-card
   problem this project already designs around for every provider).
3. **Alion**, variant 2 (same hotel, different date): departing **Warszawa**,
   30.10.2026-06.11.2026, HB, 4★ ("Kat. Eximtours 4★", "Kat. Lokalna 4★" —
   consistent this time), rating 8.6 (12 opinii), cena razem 2958 PLN ->
   **1479 PLN/os.** — this variant *was* available, with a full price
   breakdown ("Zaliczka 592 zł"). URL:
   `.../alion-1145009.html?od-2026-10-30,7-dni,HB,z-warszawy,srcx_v2_auction`.

**Business-filtered search result (<=1500 PLN/os., AI/HB/FB, 3★+, ocena
8.0+, departing Katowice/Łódź/Warszawa/Wrocław):** exactly **6** real offers
existed at search time — Meridian (Bułgaria) x2, Alion (Albania) x3 date
variants, Pebbles Resort (Malta). **Zero were from Turcja/Egipt/Tunezja/Grecja**
— the only 4 countries `wakacje_data.COUNTRIES` mapped at the time. This was
the single most load-bearing finding of the session (see §17/§19).

**Malta:** seen only as a display name on two listing cards (hotels "Soreda",
"Pebbles Resort") — never clicked into, never seen as a URL/slug. Deliberately
not added to `COUNTRIES` (see §19) for lack of raw evidence.

## 17. `z-warszawa-chopin` standalone — confirmed to NOT work (301)

Two-request live diagnostic (`robots.txt`, then
`GET /wczasy/?z-warszawa-chopin` via the raw `UrllibTransport` so the
non-200 response's headers stayed inspectable — `RequestBudget` itself
discards them on a non-200 raise): **`301`**, `Location: /wczasy/` — the
filter is silently **dropped**, not renamed or preserved. Not a canonical
redirect to an equivalent URL; a redirect to the plain, unfiltered baseline.
Confirmed same-origin, confirmed robots-legal target, confirmed via reading
the raw response headers, not guessed. **Conclusion:** `z-warszawa-chopin`,
real and UI-confirmed as it is, is not usable as a standalone single-flag
filter the way this provider's `CONFIRMED_AIRPORT_SLUGS` entries must be.
Never added.

## 18. `z-warszawy` standalone — confirmed to work exactly like `z-wroclawia`

Two-request live test: `robots.txt`, then `GET /wczasy/?z-warszawy` ->
**`200`**, no redirect. **10/10** returned offers had
`departurePlaceCode == "WAW"`; zero WMI, zero RDO, zero other codes in the
sample. Countries in the sample: turcja (7), egipt (3) — both already
mapped. This is the same quality of evidence `z-wroclawia` had. **Decision:**
`"WAW": "z-warszawy"` added to `CONFIRMED_AIRPORT_SLUGS` (§19).

## 19. Implementation session B — WAW added, `COUNTRIES` extended

- `CONFIRMED_AIRPORT_SLUGS` gained `"WAW": "z-warszawy"` (§18's live evidence).
  `z-warszawa-chopin` (§17) explicitly documented as confirmed-but-unusable.
- `wakacje_data.COUNTRIES` gained: `"albania": "AL"` (two real fetched offer
  URLs, §16, item 2/3), `"bulgaria": "BG"` (already-recorded evidence, §11.4
  of this file), `"hiszpania": "ES"` (§11.4 *and* a real fetched offer URL,
  §16 item 1). **Malta deliberately excluded** — no slug evidence, only a
  display name (§16).
- `config.json`: `providers["wakacje.pl"].max_requests` raised `5 -> 8`
  (exact count for robots + baseline + 2 confirmed airports x 3 pages each).
  `enabled` stayed `false`; `max_pages` stayed `3`.
- Full test suite green after updating `tests/test_wakacje.py` (now expects
  two confirmed airports, WAW before WRO per `filters["airports"]` order) and
  `tests/test_wakacje_data.py` (new country-mapping tests, a Malta-exclusion
  test, and a business-regression test built from §16 item 3's exact,
  non-fabricated Alion/Albania/WAW numbers, proving it now passes
  `matches()`).

Current, authoritative state: `CURRENT_STATE.md` §1-§3.

## 20. Manual UI re-confirmation: Łódź and Katowice slugs (multi-filter only)

Repeated the "Jak i skąd?" -> clear -> select-one-city -> search flow for
Łódź and Katowice in isolation (fresh browser state this time). Both
reproduced exactly:
- Łódź -> `z-lodzi`, `https://www.wakacje.pl/wczasy/?od-2026-09-22,samolotem,z-lodzi&src=fromSearch`
- Katowice -> `z-katowic`, `https://www.wakacje.pl/wczasy/?od-2026-09-22,samolotem,z-katowic&src=fromSearch`

Neither city showed a "Pokaż lotniska" sub-expander (unlike Warszawa) — each
is a single, simple checkbox.

**Still not done, and the load-bearing open item for the next session:**
neither slug has been live-tested as a **standalone** single-flag request the
way `z-wroclawia` and `z-warszawy` were (§12.1, §18). Given `z-warszawa-chopin`
(§17) proved that a slug being real and UI-generated does *not* guarantee it
works in isolation, **do not add LCJ or KTW to `CONFIRMED_AIRPORT_SLUGS`
without this live confirmation first.** Exact plan: `CURRENT_STATE.md` §5.

## 21. `z-lodzi` and `z-katowic` standalone — both confirmed to work (2026-09-22)

Three-request live test, same shape as §18: `robots.txt`, then `GET
/wczasy/?z-lodzi`, then `GET /wczasy/?z-katowic`. No pagination, no other
filter combined, no redirect followed.

- `robots.txt` -> `200`; unchanged, still allows `/wczasy/` and the
  single-flag query shape (`robots_policy` accepted both paths, crawl-delay
  floor `0.0`).
- `GET /wczasy/?z-lodzi` -> **`200`**, no `Location` header. **10/10**
  returned offers had `departurePlaceCode == "LCJ"`; no other code in the
  sample.
- `GET /wczasy/?z-katowic` -> **`200`**, no `Location` header. **10/10**
  returned offers had `departurePlaceCode == "KTW"`; no other code in the
  sample.

Both are the same quality of evidence `z-wroclawia` (§12.1) and `z-warszawy`
(§18) had: a clean `200`, no redirect, and an exclusive, matching
`departurePlaceCode` across every returned record. Unlike `z-warszawa-chopin`
(§17), neither filter was silently dropped or redirected. **Decision:**
`"LCJ": "z-lodzi"` and `"KTW": "z-katowic"` added to `CONFIRMED_AIRPORT_SLUGS`.

### Implementation session C — LCJ and KTW added, all four target airports now confirmed

- `CONFIRMED_AIRPORT_SLUGS` gained `"LCJ": "z-lodzi"` and `"KTW": "z-katowic"`
  (this section's live evidence), alongside the existing `"WAW": "z-warszawy"`
  and `"WRO": "z-wroclawia"`. `wakacje.py`'s module docstring and the dict's
  own comments were updated to match (WMI remains the only configured
  airport with no confirmed slug; `z-warszawa-chopin` and
  `z-warszawy-radom` remain explicitly excluded, per §17/§13e).
- `config.json`: `providers["wakacje.pl"].max_requests` raised `8 -> 14`
  (robots 1 + baseline 1 + 4 confirmed airports x 3 pages each = 14).
  `enabled` stayed `false`; `max_pages` stayed `3`;
  `request_gap_seconds`/`timeout_seconds`/`cycle_seconds`/`interval_seconds`
  untouched.
- `tests/test_wakacje.py` updated the same way it was updated when WAW was
  added (§19): `confirmed_airport_pages()` now returns LCJ/WAW/KTW/WRO page
  responses in that order (matching `filters["airports"]` filtered to
  confirmed slugs), `UNCONFIRMED_SLUGS` no longer includes `z-lodzi`/
  `z-katowic`, and every test asserting airport counts, URLs or budget
  behavior was extended from two to four confirmed airports. No new test
  functions were needed for the ranking/Łódź-priority requirement —
  `tests/test_filters_ranking.py::test_ranking_preferences` (parametrized
  with `departure_airport="LCJ"` against a WRO baseline) already proves,
  against the real `config.json` `ranking.airport_groups`, that LCJ now
  outranks WRO.
- Full test suite green: `pytest -q` (1035 passed, same count as before —
  only existing tests were extended, no new test functions added), `ruff
  check .` clean, `ruff format --check .` clean, `mypy` clean (70 source
  files).
- `enabled` stayed `false` throughout; no live request was made during this
  implementation session (only the three requests recorded above, before any
  code change); no DB write, no Telegram message, no commit/push.

## 22. Full-provider live test (real `RequestBudget`) — `cycle_seconds` was too small, then fixed

A controlled live run of the real `WakacjeProvider.fetch()` (real
`robots_policy`, real `RequestBudget`, real clock/sleep -- i.e. real
`request_gap_seconds=5` throttling, not a test double) against the then-current
config (`max_requests=14`, `cycle_seconds=60`, `max_pages=3`, all 4 confirmed
airports) surfaced a deterministic blocker: `provider.fetch()` raised
`ValueError("ITAKA cycle deadline exceeded")` from inside `RequestBudget.get()`
after only 10 of the planned 14 requests -- WRO's confirmed pages were never
even reached.

Root cause, from `RequestBudget.get()` (`providers/http.py`): each call after
the first sleeps `request_gap_seconds` before firing, so a full 14-request
scan requires 13 mandatory gaps (`13 x 5s = 65s`) plus the cumulative real HTTP
response time for all 14 requests -- comfortably more than the `cycle_seconds
= 60` deadline the budget was constructed with. This is deterministic, not
flaky: it reproduces on every cycle at the old values, since `scheduler._fetch()`
(`scheduler.py`) catches the exception as an isolated adapter failure, discards
every offer collected that cycle (the `offers` list is local to `fetch()`),
and schedules an exponential backoff retry -- which would fail identically
every time under the old configuration.

**Fix (implementation session D, same day):** `config.json`:
`providers["wakacje.pl"].cycle_seconds` raised `60 -> 120`. Chosen as a simple,
conservative round number comfortably above the 65s mandatory-gap floor plus
real per-request HTTP time (well under the 10-request run's own elapsed time)
and a margin, without being absurdly high. No other field changed
(`max_requests` stays `14`, `max_pages` stays `3`, `request_gap_seconds` stays
`5`, `enabled` stays `false`). No production code (`wakacje.py`, `http.py`,
`scheduler.py`) was touched -- this is a pure timing-budget configuration fix.

`tests/test_wakacje.py` gained
`test_full_confirmed_scan_fits_within_the_configured_cycle_seconds`: it drives
the real `WakacjeProvider.fetch()` against a `FakeTransport` (no network) with
a controlled clock/sleep pair (the same technique as
`test_itaka.py::test_request_spacing_and_deadline`) that simulates the real
`request_gap_seconds` spacing without actually waiting, and asserts all 14
requests complete. Verified this test fails against the old `cycle_seconds=60`
(raises the same `"cycle deadline exceeded"` after 12 simulated requests) and
passes at `120`, before being left in the suite at the new, fixed value.

No live request was made while implementing this fix (only the earlier §21
live test surfaced the problem); no DB write, no Telegram message, no
commit/push.

## 23. Final full-provider live scan — all 4 airports together, `cycle_seconds=120` confirmed

Final controlled live test of the real `WakacjeProvider.fetch()` end to end
(real `robots_policy`, real `RequestBudget`, real `request_gap_seconds=5`
throttling), against the now-fixed config (`cycle_seconds=120`,
`max_requests=14`, `max_pages=3`, all 4 confirmed airports).

**First attempt:** stopped at request #11 (`?str-3,z-katowic`) on a
`TimeoutError` at the TCP/TLS socket level -- an ordinary transient network
condition, not a robots violation, not a redirect, not a 4xx/5xx, and *not*
the `cycle_seconds` deadline (only ~65-70s had elapsed at that point, well
under the 120s budget -- the fix itself was not implicated). The live test
script's own exception handling only caught `ValueError` at the time, so the
script crashed with an unhandled traceback instead of reporting cleanly; this
was a gap in the test script, not in production code.

**Second attempt** (script fixed to also catch `TimeoutError`/`OSError`,
production code unchanged): completed cleanly, **14/14 requests**, total
elapsed **81.01s** (comfortably under the 120s `cycle_seconds` budget). All
four confirmed airports were queried together for the first time and each
returned exclusively its own `departurePlaceCode` across all 3 pages: LCJ,
WAW, KTW, WRO. 98 offers collected before deduplication, 87 after (only true
duplicates collapsed, keyed by `variant_identity`).

**New country evidence:** the raw `place.country.slug` `"cypr"` appeared on 2
records this scan, previously unmapped (`normalize_offer` produced
`country=None` for both, correctly fail-closed). This is genuine, real
live-observed evidence -- not a guess -- so `wakacje_data.COUNTRIES` gained
`"cypr": "CY"` (implementation session E, same day). `filters.country_min_stars`
has no entry for `CY`, so it falls back to the plain `min_stars=3` default,
same as any other unlisted country -- no new business rule was added.

No offer on any of the 4 confirmed airports fell within the 1500 PLN/person
budget in this scan (today's real prices were all materially higher), so
`matches()` had no candidate to accept or reject on business grounds this
run; the whitelist/price/variant mechanisms were otherwise re-confirmed
structurally (see the live session's own report for the full breakdown).

`tests/test_wakacje_data.py::test_confirmed_country_slugs_map_to_iso_codes`
gained the `("cypr", "CY")` case; the existing
`test_unknown_country_slug_still_yields_none_and_fails_closed` already covers
the "still fails closed for anything not in `COUNTRIES`" requirement, so no
new test was needed for that half.

No further live request was made while implementing this fix. No DB write, no
Telegram message, no commit/push.

Current, authoritative state: `CURRENT_STATE.md` §1-§5.
