# AGENTS.md

Engineering instructions for coding agents and contributors working in this repository.
Keep changes small, scoped and consistent with the existing code.

## Project purpose

Travel Deal Agent is a small local Python application that watches Polish tour-operator
sites (Wakacje.pl, TUI, ITAKA; Rainbow is implemented but disabled) for package holidays,
ranks matching offers, keeps price history in SQLite and sends alerts through Telegram. It
runs on Windows or in Docker on a Linux VPS. See [README.md](README.md) and
[DEPLOYMENT.md](DEPLOYMENT.md).

Do not introduce frameworks without a concrete need.

## Architecture

Keep these responsibilities separate; inject external dependencies (HTTP transports,
clocks, sleep, notifiers) so tests control them.

- **Provider modules** (`providers/`): source access and parsing, one adapter per source.
- **Shared `Offer` model** (`models.py`): the only type that crosses the provider boundary.
- **Normalization:** provider-specific quirks are resolved inside the adapter.
- **Filtering and watchlist** (`filtering.py`, `watchlist.py`): pure eligibility rules.
- **Scoring** (`ranking.py`, `attractiveness.py`): ordering and classification.
- **Storage** (`storage.py`): SQLite snapshots, price history, outbox.
- **Notifications** (`notifications.py`, `notification_content.py`): transport-independent
  message content; Telegram (official Bot API) and console transports.
- **Scheduler** (`scheduler.py`): due times, backoff, active hours, failure isolation.

Business rules live in `config.json`, not in code. Provider rating scales stay native
(for example ITAKA 1-6, Wakacje.pl 0-10); never convert them to a common scale for
filtering.

## Provider development rules

- Prefer plain HTTP/HTML parsing; use Playwright only where browser behavior is required.
- Respect `robots.txt`, rate limits (request budgets and gaps) and timeouts. Never bypass
  CAPTCHAs, logins or blocks.
- Isolate provider failures: one provider failing must not stop the others. Log errors at
  integration boundaries, and do not disguise database failures as source errors.
- Make no assumptions about undocumented price semantics (per person vs. total, fees
  included or not); verify them against real data and record the finding.
- Record provider reconnaissance in `experiments/<provider>/` when a new behavior is
  investigated. Live requests are for controlled, bounded reconnaissance only.
- Add one real provider integration at a time, with fixtures covering it.

## Data and model rules

- Use the common `Offer` model; use `Decimal` for money.
- Make `price_per_person` versus `total_price` explicit; never infer one from the other
  silently.
- Keep offer identities stable across scans.
- Do not match hotels fuzzily. Matching is exact after normalization; list aliases in
  configuration instead.
- Unknown or ambiguous values fail closed rather than pass a filter.

## Watchlist rules

- Watchlist limits are separate from the standard filters (`filters.max_price` does not
  apply to a watched hotel).
- Provider-specific identifiers (listing URLs, destination codes, slugs) belong in
  configuration, never in code.
- Final entry-specific validation (price, nights, airport, country) always applies to
  every candidate, however it was found.

## Code style

- Type all functions and data boundaries; strict mypy is enabled for code and tests.
- Comments and docstrings explain *why* (provider quirks, robots/rate-limit decisions,
  identity rules, non-obvious business rules), not what the next line does.
- Code, comments and documentation are in English.

## Testing

```
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy
```

- Tests use fixtures, temporary databases and injected clocks, with clear
  arrange/act/assert sections, and assert public behavior rather than SQL details.
- Unit tests never access the network.
- Keep fixtures independent of the production `config.json`.

## Security

- Never commit `.env`, tokens, API keys, SSH keys or other secrets.
- Do not log secrets or user data; keep the Telegram token out of error messages.
- Example files (`.env.example`, `hotel_watchlist.example.json`) contain placeholders only.
- Keep local databases, caches, logs and personal configuration (`hotel_watchlist.json`)
  out of version control.
- Do not commit real VPS addresses or local private paths.

## Git and change safety

- Inspect the diff before committing.
- Do not run destructive commands (`reset --hard`, `clean`, forced checkouts) on
  uncommitted work.
- Keep provider changes scoped to that provider.
- Do not commit unrelated or generated files.

## Definition of done

- Targeted tests for the change pass, then the full test suite.
- Ruff lint, Ruff format check and mypy are clean.
- Relevant documentation (README, DEPLOYMENT, experiments notes) is updated.
- No secrets, personal data or local paths leaked into code, docs or logs.
