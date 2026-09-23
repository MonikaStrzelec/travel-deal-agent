# Rainbow listing variant investigation

## Scope and evidence

One controlled live run on 2026-09-20, following offline inspection of existing
HTML, request URL logs and the production parser. Production code, ranking and
the PLN 1500 default were not changed. The run used the established filters with
a diagnostic PLN 2000/person limit, did not scroll or open product pages, and
inspected two cards. No API requests were replayed or invented.

Local evidence is under `data/rainbow-production/variants-20260920T145903Z/`:
`report.json`, `search-response.json`, `cards-response.json`, and two card HTML
fragments. The browser fetched its normal first-page batch of ten cards; the
investigation concerns only Gardenia Hotel and Alaiye Kleopatra. The website
reported 14 matches. No 403/429 response or human-verification block was observed.

## Data sources

1. Saved card HTML exposes a summary and a product-only anchor. JSON-LD also
   contains summary pricing, not a matrix of priced variants. The saved Nuxt
   hydration payload belongs to the initial home page and does not establish
   the current filtered variants.
2. A read-only inspection of Vue component props on the two live card nodes
   returned no component data. This does not prove application state is absent;
   no dependable application-state extraction contract was established.
3. The listing makes `POST /api/wyszukiwarka/v5.0/wyszukaj`, followed by
   `POST /api/bloczki/v5.0/pobierz-bloczki`. These are the best evidenced sources.
   The first response supplies `Wynik[].Id`, `UnikalnyKluczOferty`, `Cena`,
   `TerminWyjazdu`, `LiczbaDni`, and a search count. The second request includes
   these search records in `Parametry`, plus party and room information.
   Card responses join through `Klucz == Wynik[].Id` and the same opaque key.
4. Tooltip interactions were not tested. Nothing in the captured card response
   establishes that hovering a summary exposes separately priced variants.

## Example: Gardenia Hotel

- Card key: `6466_12682:249522:10474247`.
- Opaque offer key:
  `AkIZAooxA7LOAwEBAwfTnwEBAjE5ASMBAgEBAQEBAQABAgNm/REBAgOX/REBAgECAQc=`.
- Price: `Cena.Cena = 1551`, `Cena.CzyCenaZaOsobe = true`.
- Date: 2026-12-05; duration: 8 days / 7 nights.
- `Przystanki`: KTW / Katowice (ID 1178982), WAW / Warszawa Chopin
  (ID 1179074).
- `Wyzywienia`: HB / 2 posilki (`UslugiId=[2]`), AI / All inclusive
  (`UslugiId=[1]`).
- `BazoweInformacje.OfertaUrl` contains the product path with
  `unikalnyKluczOferty`, `liczbaPokoi`, `czyCenaZaWszystkich` and repeated `wiek`
  parameters. This is a source-generated candidate deep link, not a fabricated
  URL. Its destination/preselection was not tested.

These are separate option lists, NOT four verified combinations. The response
does not attach a price or opaque key to each airport/meal combination. Do not
form a Cartesian product or copy PLN 1551 to every combination.

Alaiye Kleopatra independently has the same structure: PLN 1602/person,
2026-12-12, 8 days / 7 nights, WAW and KTW, HB and AI, one opaque key and one
source-generated parameterized URL. It does not resolve the ambiguity either.

## Unresolved fields and semantics

- No explicit priced airport/meal variant rows were found in either response.
- No return date was found for the inspected records.
- Card `TerminWyjazdu` and airport `DataWyjazdu` contain `13:13:00Z` for both
  airports on both inspected cards; search dates contain `00:00:00Z`. These
  timestamps must not be promoted to flight times without validating their
  meaning. They may be placeholders; that remains an inference.
- The opaque key is a useful source identity candidate, but stability across
  price changes, party changes and repeated scans is untested. Neither it nor
  `KluczGrupy` has been proven to identify a fully specified booking variant.
- No room selection, complete charges or final booking availability was verified.

## Recommended implementation direction

Prefer passive capture of the listing's existing two JSON responses through the
browser, with no extra per-card requests. Correlate response requests to the
current filters and party; wait for the matching response bodies and settled
listing, and join by source keys rather than list position. Parse only the
configured small candidate budget. Keep the source URL and opaque key, raw
airport/meal option lists, and provenance, without marking a variant verified.

Do not depend on Vue internals, guess the binary key format, or assign the
minimum card price to every option. A direct HTTP adapter is a later possibility,
but standalone request/session requirements have not been tested.

A further explicitly authorized diagnostic is needed before implementing complete
variants: inspect one card interaction and, if it adds no priced variant rows,
one source-generated deep link and the requests its selection controls make.
The purpose is to discover the structured variant resolver and verify the opaque
key's airport, meal, price and URL semantics, not to crawl product pages.
No further live run was performed in this investigation.

## Separately authorized one-card follow-up (2026-09-20 15:03 UTC)

Exactly one follow-up run was attempted. It stopped during initial airport
selection, before any Gardenia card interaction or detail navigation:
`RainbowStructureError: Missing or ambiguous Rainbow control` from
`BrowserListing.unique()` while checking the first configured airport (LCJ).
No retry was performed. The diagnostic budget was PLN 2000; production remains
PLN 1500. No production-provider or ranking change was made.

Evidence: `data/rainbow-production/one-card-20260920T150353Z/report.json`,
and the browser snapshot in
`data/rainbow-production/2a073dd9e32044f59487acaf3b70ac98/`.
The saved HTML did not yet include the airport checkbox panel, while the later
screenshot showed that panel opening, including Lodz. This is consistent with
an immediate `locator.count()` check racing the asynchronous panel rendering;
it is not proof that the airport label changed. The exact count at failure was
not instrumented.

There were no recorded 403/429 responses. No tooltip, airport/meal selection on
a card, source deep link or variant resolver was tested in this follow-up.
Consequently it adds no variant records, joining keys or evidence of passive
variant resolution beyond the previous findings. Listing observations remain
unverified. Any new live attempt requires separate authorization and should
first address waiting for the airport panel's controls to become available.

## Synchronization fix and single follow-up (2026-09-20 15:08 UTC)

The airport panel now waits for its visible source-specific container and
checkbox group, no visible skeleton/busy indicator, and attached/enabled
configured controls before selection. Dynamic sidebar controls also wait for
attachment before the uniqueness check. No fixed sleep or positional fallback
was introduced. Three offline regression tests cover delayed attachment,
panel readiness order and a clear panel timeout. All 573 tests, Ruff checks and
strict mypy passed.

Exactly one new live run was performed after this fix. Airport selection passed:
the final URL contains LCJ, WAW, WMI, KTW and WRO. The run then timed out in the
initial listing readiness check, before selecting sidebar filters or inspecting
Gardenia. Both saved URL and the sort input show `rekomendowane-biznes-desc`,
whereas the existing readiness condition requires `cena-asc`. The page displayed
2834 results for those incomplete filters and called
`/api/rekomendacje/v5.0/wyszukaj`; this is not evidence of a variant resolver.

Evidence: `data/rainbow-production/one-card-20260920T150846Z/report.json` and
`data/rainbow-production/43d64328701c42f8be868f0d95dad568/` (HTML, URL, screenshot).
No 403/429 was recorded. No card interactions, deep-link navigation or booking
actions occurred. No further live attempt was made, and no sorting or resolver
implementation was added. The production budget remains PLN 1500. Variant
findings and the unverified status of listing observations remain unchanged.

## Final sorting fix and one-card investigation (2026-09-20 15:13 UTC)

Sorting is now enforced after all filter updates. Intermediate readiness checks
wait for the requested filters and settled listing without requiring an order
that the site may reset. Final readiness still requires both URL and sort input
to equal `cena-asc`. If necessary, the observed sort container's combobox selects
the option named `od najniższej ceny`. No click occurs when URL and UI already
agree. Airport synchronization is unchanged. All 576 offline tests, Ruff lint,
format checks and strict mypy passed.

Exactly one live run reached Gardenia after all filters and final sorting passed.
Tooltip hover attempts were intercepted by the mobile-app promotional iframe;
therefore tooltip data availability remains untested. The probe then opened only
Gardenia's source-generated deep link, without booking or changing configuration.

### Confirmed structural source: detail-page Nuxt hydration

The saved detail HTML contains `script#__NUXT_DATA__`, a reference-indexed Nuxt
payload. After resolving its reference table, `pinia.kartaHotelu.kalkulator`
contains `KluczOferty`, `Wybrana`, `Polaczenia`, `PrzystankiWyjazdowe` and `Bloki`.
`pinia.kartaHotelu.flightInfo` describes the selected flights. Numeric reference
indices below document this artifact only; they must never be hardcoded in a
parser.

The selected record is now tied to the exact listing key:

- `KluczOferty.KluczProduktoHotelWC`: `6466_12682:249522:10474247`.
- `Wybrana.UnikalnyKluczOferty` equals the listing and deep-link key.
- `Wybrana.CenaAvg`: 1551 PLN; `CenaSum`: 3102 PLN.
- `Wybrana.RezerwujParametry` explicitly specifies departure stop 1178982,
  return stop 1179031, `wybraneWyzywienie=2-posilki`, two adults and zero children,
  start 2026-12-05 and seven nights. This string was read, not navigated to.
- The active `Polaczenia` record maps those stops to KTW / Katowice outbound and
  return, and carries the same selected opaque key.
- Selected room: economy, `TypPokojuId=14641`, `KonfiguracjaId=35`, one room.
- Return date: 2026-12-12, present in flight data and visible selected summary.
- Selected source URL: the exact parameterized Gardenia deep link already
  supplied by the listing. Its selected summary agrees with this configuration.

The payload also exposes genuine alternative-choice records, not a Cartesian
product: WAW with its own opaque key and raw `Doplata=702`; All inclusive with
its own opaque key and raw `Doplata=980`. These are contextual alternatives, not
verified final prices. Do not add these amounts to the card price or combine
them without establishing the calculator's units and selection semantics. Even
the active HB choice has a different opaque key encoding from `Wybrana`, so
keys from different record types must not be assumed equivalent.

Both flight legs explicitly set `CzyWyswietlacGodzine=false`, with no flight
number and `13:13:00Z` timestamps. Retain the date only; do not expose that time
as a confirmed flight schedule.

### Network evidence and diagnostic limitation

Observed requests include
`GET /api/szczegoly-wycieczki/v5.6/pobierz-informacje-o-pokojach`
with `hotelId=6466`, `termin=2026-12-05`, `czyDynamic=false`, plus faculties and
notes endpoints. No separately called full calculator resolver was identified;
the selected calculator data is embedded in the detail document itself.

The run ended with a diagnostic export error: Playwright could no longer read
a response body from before navigation (`Network.getResponseBody: No data found
for resource with given identifier`). The detail HTML, text, screenshot, request
metadata and room response were saved before that failure. Thus the capture is
partial, not a fully successful network export. No retry was performed.

Evidence: `data/rainbow-production/one-card-20260920T151320Z/`, particularly
`detail.html`, `detail.txt`, `report.json`, and `response-01.json`.

### Implementation assessment

The exact selected variant can be resolved from one product document without
booking. It cannot currently be obtained passively from listing responses alone:
detail navigation is an additional document request for each candidate. Prefer
a bounded enrichment of a few listing candidates, parse named hydration fields
and cross-check the selected opaque key, product key, stops, meals, party and
prices. Keep alternative-choice tokens and surcharges separate from complete
variants. No resolver implementation was added in this change.

The selected configuration is evidenced, but complete mandatory costs, long-term
key stability, alternative-price semantics and independent API access remain
unverified. Another live run is not needed to recognize this selected record;
future live validation of an implemented resolver or alternative selection would
need separate authorization. Production budget remains PLN 1500.
