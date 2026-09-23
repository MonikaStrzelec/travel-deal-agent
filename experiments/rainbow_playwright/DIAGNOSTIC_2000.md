# One-off Rainbow card diagnostic: 2026-09-20

Exactly one authorized live session ran with a **PLN 2000/person diagnostic cap**.
The business cap and `settings.json` remain PLN 1500/person. No production provider
was changed. Zero results remain a valid terminal state and never authorize automatic
filter relaxation.

Artifacts: `data/rainbow-playwright/20260920T135246Z/` (ignored local files).
The browser closed normally. No offer details or pagination were opened. There was
one short scroll back to the first cards after filters; no full-list traversal.

## Confirmed filters

Final URL and DOM evidence (`19.html`, `19.txt`, `20-inspection.json`) confirm:

- Two adults, no children, one room: two adult query entries, `dzieci=nie`,
  `liczbaPokoi=1`, and the visible `2 osoby, 1 pokój` summary.
- Unrestricted dates: `Kiedykolwiek` and empty date query fields.
- LCJ, KTW, WAW, WMI and WRO, selected in the departure panel before initial search.
- 7–9 days; All inclusive, 3 posiłki and 2 posiłki; customer rating from 5.0.
- All three separate 3-star, 4-star and 5-star checkboxes checked.
- Price input 2000, `Cena za osobę` checked, `cena=avg`, `cena.do=2000`.
- Ascending price confirmed in UI and `sortowanie=cena-asc`; no sorting click.

## Observed star mapping

The run selected 5 stars, then added 4, then added 3. Each action clicked the
checkbox's associated visible `label[for=...]`, verified the input was checked,
and waited for the URL and listing update. No force-click was used.

| Minimum | Selected checkboxes | Observed repeated `standardHotelu` values | Snapshot |
| --- | --- | --- | --- |
| 5 | 5 stars | `10` | `11` |
| 4 | 4 and 5 stars | `10`, `8` | `12` |
| 3 | 3, 4 and 5 stars | `10`, `8`, `6` | `13` |

The live label/input evidence associates 3 stars with `6`, 4 with `8`, and 5 with
`10`. These are source option codes, not numerical star ratings or a general
arithmetic conversion. Input IDs were `Standard hotelu6`, `Standard hotelu8` and
`Standard hotelu10`; they were read from the controls, not guessed.

## Waiting and result count

Every sidebar action waited before the next action: expected URL value, absent
`.szukaj-wyniki .r-skeleton-bloczek-szukaj`, and a stable URL/count/first-three-card
text signature for one second using bounded `wait_for_function` polling. Nonempty
results also required ascending sorting in UI. The explicit no-results message
was accepted as an alternative terminal state without requiring the missing sort UI.
There was no fixed sleep. Text stability remains a bounded heuristic; delayed-update
and identical-results edge cases still need tests in a future browser adapter.

The website reported **14 offers**, rendered 10 cards in the initial batch, and only
the first **3** were extracted. No recommendations were substituted for results.

## First three cards

All three cards show Turkey / Turkish Riviera, 4 stars, 8 days / 7 nights,
and `2 posiłki (+1)`. Preserve alternatives rather than assuming one exact variant.

| Hotel | PLN/person | Native rating | Reviews | Departure | Airport text |
| --- | --- | --- | --- | --- | --- |
| Gardenia Hotel | 1551 | 5.3/6 | 44 | 2026-12-05 | Katowice (+1) |
| Alaiye Kleopatra | 1602 | 5.4/6 | 78 | 2026-12-12 | Warszawa Chopin (+1) |
| Riviera Hotel and Spa | 1779 | 5.5/6 | 41 | 2026-12-05 | Katowice (+1) |

Card extraction scoped each selector to `[data-test-id^="r-bloczek:szukaj:"]`.
Each of the following selectors matched exactly once per inspected card:

| Field | Relative selector / attribute |
| --- | --- |
| Hotel | `[data-test-id^="r-typography:szukaj:tytul-"]` |
| Country/direction | `[data-test-id^="r-typography:szukaj:lokalizacja-"]` |
| Price/person | `[data-test-id^="r-typography:szukaj:cena-aktualna-"]` |
| Stars | `[data-test-id^="r-gwiazdki:szukaj:"]`, `data-rating` corroborated by `aria-label` |
| Customer rating and review count | `[data-test-id^="r-typography:szukaj:ocena-"]`, text and `aria-label` |
| Meals | `[data-test-id^="r-typography:szukaj:wyzywienie-"]` |
| Departure date, days and nights | `[data-test-id^="r-typography:szukaj:termin-wyjazdu-"]` |
| Airport | `[data-test-id^="r-typography:szukaj:przystanek-"]` |
| Offer URL | Enclosing anchor's `href` |

URLs read without navigating:

- `https://r.pl/turcja-riwiera-wczasy/gardenia-hotel`
- `https://r.pl/turcja-riwiera-wczasy/alaiye-kleopatra`
- `https://r.pl/turcja-riwiera-wczasy/riviera-hotel-and-spa`

No explicit return date or departure time appeared in the inspected card text.
Both remain null; a return date was not fabricated from nights/days. Links are general
product URLs, not full room/flight/meal variant links. Alternative airport/meal suffixes
mean the card summary does not confirm the exact variant behind the price. Prices are
listing quotes, not verified complete booking totals.

`cards.json` contains normalized diagnostic values, and `20-inspection.json` retains
raw values, card HTML and selector counts. Offline checks confirmed unambiguous
selectors, parseable dates/ratings, ascending prices and the diagnostic thresholds
for all three cards. This validates one diagnostic extraction, not a production parser
or a completed Rainbow provider.

A mobile-app promotion modal obscured the last viewport screenshot (`22.png`).
The DOM extraction had already completed; this run did not test dismissing that modal.

## Remaining work

Implement a reusable parser with independent offline fixtures, missing/ambiguous-field
cases, recommendation exclusion and variant semantics. Implement the readiness adapter
with explicit empty-result success and label-based star selection. Full variant URLs,
return dates, departure times and complete booking totals remain unverified.
