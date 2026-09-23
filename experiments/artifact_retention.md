# Proposed diagnostic artifact retention (not implemented)

Current ITAKA runs create timestamp directories under `data/itaka-playwright/` with
HTML, PNG and accessibility TXT snapshots, request/command logs, optional error text,
and listing/collection JSON outputs. There is currently no automatic retention.
No existing artifacts are deleted by this proposal.

For each future POC source, retain at most the latest five successful runs and the
latest five failed runs, for no longer than 14 days. Also enforce a 500 MiB aggregate
diagnostic budget per source: remove oldest completed runs until within budget,
even when count/age limits would otherwise retain them. This caps failure storms too.

Write a small completion manifest with status, timestamps, counts and stop reason.
Clean only completed runs after closing files/browser, using an explicit allowlist:
`data/itaka-playwright/`, `data/rainbow-playwright/`, and a future dedicated Wakacje.pl
diagnostic directory. Resolve paths and reject links or paths outside these roots.
Never delete an active run. Existing directories without a manifest require a separate
reviewed migration; do not guess their completion state.

For later unattended operation, keep compact bounded summaries for successful runs;
capture full HTML/PNG only on errors or an explicitly enabled diagnostic sample.
Add per-run file/request-log size limits and stop diagnostic capture at the budget
instead of letting an active run grow indefinitely. Rotate application logs separately.
Count limits alone do not bound disk usage; both byte and age limits are needed.

Manual Codegen recordings are evidence, not completed POC runs: review them separately,
extract small sanitized offline fixtures into the repository when useful, then apply
an explicit age/size policy to the recordings. Do not silently include them in a
timestamp-directory cleanup routine.

`data/offers.sqlite3`, any configured database path, SQLite WAL/SHM files, price history
and backups are durable application data and are NEVER cleanup targets. Never remove
the entire `data/` directory or use broad file-extension deletion there. Database
retention and backup policies are separate from disposable browser diagnostics.
