# Rainbow: single controlled diagnostic run, 2026-09-20

One browser session completed. No repeat run, offer-detail visit, pagination, manual
full-page scrolling or production-provider change was performed. Local artifacts are
under ignored `data/rainbow-playwright/20260920T134039Z/` (HTML, viewport screenshots,
accessibility snapshots, commands, request log and exact final URL).

## Result and filter confirmation

The final page explicitly reported **Brak wyników** and
**Dla tak zawężonych kryteriów nie znaleźliśmy ofert.** This is a valid zero-result
search, not a Rainbow failure. Filters were not relaxed.

All requested selections were confirmed by final URL plus checked controls/filter
chips: LCJ, KTW, WAW, WMI, WRO; 7–9 days; All inclusive, 3 posiłki and 2 posiłki;
customer rating from 5.0; maximum PLN 1500 with **Cena za osobę** selected.
No hotel-star checkbox was selected and the URL has no `standardHotelu` parameter.
`ocenaKlientow=10-12` was observed with **Od 5.0**; this does not define a general
rating conversion. `sortowanie=cena-asc` remained in the final URL.

The page also rendered 10 recommendation cards below a separate recommendation
heading. They are not matching search results. The final 1440×1000 screenshot shows
one complete recommendation and the beginning of another. Do not collect these cards
as matches or infer success from the presence of a card locator alone.

## Observed update behavior

- Initial navigation used the homepage **Szukaj** button once. URL change and document
  ready state alone exposed stale homepage content, confirming they are insufficient.
- Airport checkboxes in the top panel only changed the selection and panel summary.
  They did not update the listing/URL individually. **Wybierz** closed the airport
  panel and applied all five airports. No second **Szukaj** click was needed.
  This is an observed exception to the earlier assumption that every checkbox
  immediately applies to the listing.
- Duration, meal and rating changes applied automatically. Price was filled and blurred;
  this run does not isolate whether blur is required or merely triggers/finishes debounce.
  No global filter-submit action was used.
- Results were temporarily replaced by `.r-skeleton-bloczek-szukaj` inside
  `.szukaj-wyniki`. A MutationObserver captured insertion of this skeleton and subsequent
  changes to the result count and first three card texts. Image skeletons are unrelated.
- For sidebar updates, the diagnostic wait required the expected URL parameter,
  absence of listing skeletons, unchanged count/first-three-card text for one second,
  and ascending sorting in both URL and UI. This was bounded condition polling,
  not a fixed sleep. Each sidebar action completed its wait before the next action.
- Sorting was initially verified through
  `[data-test-id="r-select-form:szukaj-sortowanie"]` (visible text
  **Sortuj: od najniższej ceny**) and `[name="szukaj-sortowanie"]` value `cena-asc`.
  Sorting was never clicked.

## Empty-state issue in the diagnostic wait

After setting the price, the site removed the sorting control and showed its explicit
empty state. The diagnostic wait still required that control, so it timed out after
20 seconds and closed the session after saving `stopped.html`, `stopped.txt` and
`stopped.png`. `error.txt` records this harness limitation, not a failed search.
Offline inspection confirmed no listing skeleton remained and all requested filter
chips were present. No live retry was made.

The browser adapter must accept the explicit empty state as a successful terminal
branch before considering recommendation cards or requiring a sorting control:

- `[data-test-id="r-typography:szukaj-naglowek:tytul"]`: `Brak wyników`.
- `[data-test-id="r-typography:szukaj-brakWynikow:tytul"]`: the exact no-offers message.
- Expected cumulative URL filters and no visible listing skeleton.

For nonempty results, retain sorting verification plus completed-update evidence.
The one-second text-stability check is an observed heuristic, not a proof against
every delayed update. A future observer must track skeleton appearance/removal per
action, allow identical resulting cards, and handle empty states explicitly.

## Card locator evidence (not matching final offers)

There were no matching cards after applying all filters. To preserve useful evidence
without another request, the first card in the saved **pre-price** snapshot `20.html`
was inspected offline. It is Gardenia Hotel at PLN 1551/person and therefore exceeds
the requested cap. It is not a candidate or a verified booking quote.

Scope extraction to one `[data-test-id^="r-bloczek:szukaj:"]` card. Index suffixes
are positions, not offer identities. Use semantic prefixes within that card:

| Field | Observed relative selector / source | Example from pre-price card |
| --- | --- | --- |
| Hotel | `[data-test-id^="r-typography:szukaj:tytul-"]` or heading level 3 | Gardenia Hotel |
| Country/region | `[data-test-id^="r-typography:szukaj:lokalizacja-"]` | Wypoczynek • Turcja: Riwiera Turecka |
| Price/person | `[data-test-id^="r-typography:szukaj:cena-aktualna-"]` | 1 551 zł/os. |
| Stars | `[data-test-id^="r-gwiazdki:szukaj:"]`, `data-rating` and `aria-label` | 4; Ocena: 4 gwiazdki na 5 |
| Rating | `[data-test-id^="r-typography:szukaj:ocena-"]`, text/`aria-label` | 5.3/6 |
| Review count | Same rating element, `aria-label` / nested text | 44 opinie |
| Meals | `[data-test-id^="r-typography:szukaj:wyzywienie-"]` | 2 posiłki (+1) |
| Date | `[data-test-id^="r-typography:szukaj:termin-wyjazdu-"]` | 05.12.2026 |
| Duration | Same date element | 8 dni / 7 noclegów |
| Departure airport | `[data-test-id^="r-typography:szukaj:przystanek-"]` | Katowice (+1) |
| Departure time | No displayed field found in the inspected card | Unavailable |
| Offer URL | Enclosing `a.szukaj-bloczki__element[href]` | `/turcja-riwiera-wczasy/gardenia-hotel` |

The URL is a general product link, not a full dated flight/room/meal variant.
The `(+1)` suffixes indicate alternatives; do not infer the airport/meal of the
quoted price from this summary alone. Card star data does not establish filter
parameter mapping. These locators are grounded in saved DOM, but broader robustness,
optional fields and variant semantics still require offline fixtures and validation.

## Remaining work

Implement and test the actual browser readiness adapter, including the empty-state
branch and airport-panel commit behavior. Add bounded parsing and independent DOM
fixtures for the observed selectors, optional values, native ratings, days versus
nights, recommendation exclusion and aggregate cards. Full variant identity and
departure time remain unconfirmed. No production adapter was changed and no new live
run is authorized by this document.
