# Repository development guidelines

- Communicate with the project owner in Polish. Keep code, comments and documentation in English.
- Maintain a small local Python application. Do not introduce frameworks without a concrete need.
- Keep business rules in configuration; preserve source-specific native rating scales.
- Type all functions and data boundaries. Keep strict mypy checks enabled for code and tests.
- Separate source access, filtering, ranking, scheduling, storage and notification delivery.
- Inject external dependencies and clocks where tests need control.
- Unit tests must not access the network. Use independent fixtures, temporary databases and clear arrange/act/assert sections.
- Prefer public behavior over SQL implementation details in tests.
- Log errors at integration boundaries; do not disguise database failures as source errors.
- Run pytest, Ruff lint, Ruff format checks and mypy after substantive changes.
- Keep README suitable for a public portfolio: no personal paths, private data or secrets.
- Keep .env, local databases, caches and logs out of version control.
- Do not implement live scraping, connect paid services, send external notifications or publish/push without explicit authorization.
- Target travel sources are ITAKA, Rainbow, Wakacje.pl and TUI. Fully test one real integration before adding the next; do not implement them all at once.
- Telegram (official Bot API, plain HTTP request) is the primary notification channel for the MVP. Discord is the preferred channel for a possible future addition after the MVP; WhatsApp is not planned. Keep message content independent of transport. ConsoleNotifier remains available for local/dev use; never automate the WhatsApp app through unofficial methods.
