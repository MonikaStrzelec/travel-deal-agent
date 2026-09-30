# Windows setup (Task Scheduler)

Step-by-step instructions for running Travel Deal Agent locally on Windows, either
manually or unattended through Task Scheduler. For continuous 24/7 operation prefer the
Docker setup in [DEPLOYMENT.md](../DEPLOYMENT.md); see the
[README](../README.md#two-ways-to-run-it) for how the two options compare.

## Running on a new Windows computer

This section takes a fresh Windows 10/11 machine to a running agent. All commands are for
**Windows PowerShell**, run from the project folder. They use the virtual environment's
Python explicitly (`.\.venv\Scripts\python.exe`), so activating the environment is not
required.

### A. Requirements

- **Windows 10 or 11** with a normal user account (administrator rights are not needed).
- **Python 3.10 or newer**, 64-bit (CI tests 3.10 and 3.14). Install it from
  [python.org](https://www.python.org/downloads/windows/) and make sure the `py` launcher
  works: `py --version`.
- **Git**, to clone the repository ([git-scm.com](https://git-scm.com/download/win)).
- **Internet access** for installing packages, downloading Chromium once, the travel
  sources and Telegram.
- **Playwright's Chromium build** -- required because the TUI provider uses a headless
  browser. It is installed by a command in step C; no separate Chrome/Edge installation is
  used.
- A **Telegram bot token and chat ID** (optional, but needed for phone notifications; see
  [Telegram notifications](../README.md#telegram-notifications)).

Nothing else is required: no database server, Docker, Node.js or paid service.

### B. Get the project

```powershell
cd C:\path\to
git clone <REPOSITORY_URL> travel-deal-agent
cd travel-deal-agent
```

Replace `C:\path\to` with the folder where the project should live and
`<REPOSITORY_URL>` with the repository's clone URL. Every following command is run
from this `travel-deal-agent` folder.

### C. Create the virtual environment and install

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

- `requirements-dev.txt` installs the runtime dependencies plus pytest, Ruff and mypy
  (needed for the offline test in step F). A runtime-only machine can use
  `requirements.txt` instead. On Windows, `tzdata` is installed automatically.
- `playwright install chromium` downloads the browser into the **current Windows user's**
  profile (`%LOCALAPPDATA%\ms-playwright`). Run it as the same user that will run the
  scheduled task.
- Upgrading pip first is not required. If `py` is not found, use
  `python -m venv .venv` instead.
- Optional: `.\.venv\Scripts\Activate.ps1` activates the environment for interactive work.
  If PowerShell blocks it with an execution-policy error, keep using the explicit
  `.\.venv\Scripts\python.exe` paths shown here.

### D. Configuration

The business configuration is the committed `config.json` in the project root; there is no
separate template to copy. Edit it with any text editor; the file is strictly validated at
startup, so a typo or unknown key stops the program with a `Configuration error`. The most
common settings are listed in [Configuration](../README.md#configuration).

Machine-local settings and secrets go in `.env`, created from the example:

```powershell
Copy-Item .env.example .env
```

`.env.example` contains the database path, log level and config file name with working
defaults. You do not need to change anything there for a normal installation.

### E. Telegram secrets

Open `.env` and set both values (remove the leading `#`):

```dotenv
TELEGRAM_BOT_TOKEN=<your-bot-token>
TELEGRAM_CHAT_ID=<your-chat-id>
```

- Both must be set together; setting only one is a configuration error. With both unset,
  alerts are only written to the log (`Notifications: console`).
- Variables already set in Windows take precedence over `.env`.
- `.env` is ignored by Git. **Never commit it**, and never paste the token into
  `config.json`, the README, an issue or a screenshot.

See [Telegram notifications](../README.md#telegram-notifications) for how to obtain the two values.

> Tip: do the offline smoke test in step F **before** adding the Telegram secrets, so its
> fictional demo offers are not sent to your phone.

### F. First smoke test (no live requests)

1. Run the offline test suite. It blocks network sockets and browser start-up, so it never
   contacts a travel site or Telegram:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q
   ```

   Every test should pass.

2. Optionally run the application itself against the fictional `mock` provider only, using
   a temporary configuration and a separate database so the real history is untouched:

   ```powershell
   New-Item -ItemType Directory -Force data | Out-Null
   $cfg = Get-Content config.json -Raw | ConvertFrom-Json
   foreach ($p in $cfg.providers.PSObject.Properties) { $p.Value.enabled = ($p.Name -eq "mock") }
   $cfg | ConvertTo-Json -Depth 20 | Set-Content data\smoke-config.json -Encoding utf8
   $env:TDA_CONFIG = "data\smoke-config.json"; $env:TDA_DATABASE = "data\smoke.sqlite3"
   .\.venv\Scripts\python.exe -m travel_deal_agent --force
   Remove-Item Env:TDA_CONFIG, Env:TDA_DATABASE
   Remove-Item data\smoke-config.json, data\smoke.sqlite3
   ```

   Expected output: `Checking provider mock`, `Provider mock matched 3 offers`, three
   demo alerts and `MOCK DATA - not real travel offers`.

**Important:** a normal run with the committed `config.json` contacts the real sources
immediately (ITAKA, Wakacje.pl and TUI are enabled), unless the current time is outside
active hours.

### G. Manual run (one check)

```powershell
.\.venv\Scripts\python.exe -m travel_deal_agent --force
```

This performs one live check of every enabled provider and exits. `--force` ignores the
saved per-provider due times and active hours; without it, a single run only scans
providers whose next run is due (on a fresh database: all of them). Output goes to the
terminal and to `logs\agent.log`.

### H. Continuous mode

```powershell
.\.venv\Scripts\python.exe -m travel_deal_agent --watch
```

The scheduler keeps running, scans each provider when it is due and stops with Ctrl+C.
`--watch` and `--force` cannot be combined. For unattended 24/7 operation, do not keep a
terminal open; use Task Scheduler as described next. **Never run two agents against the
same database at the same time** -- the application itself holds no lock. Once the agent runs on
a VPS ([DEPLOYMENT.md](../DEPLOYMENT.md)), disable or delete the Windows Task Scheduler task, or
you get double scans, duplicate Telegram messages and double traffic to the sources.

## Running continuously on Windows (Task Scheduler)

Task Scheduler starts the agent at sign-in with `pythonw.exe`, the console-less Python
included in every Windows virtual environment. No `.bat`/`.ps1` helper is needed.

Below, `C:\path\to\travel-deal-agent` stands for **your** project folder -- replace it
everywhere.

### Create the task

1. Press the Windows key, type **Task Scheduler** and open it.
2. In the right-hand **Actions** pane, click **Create Task…** (not "Create Basic Task",
   which hides options needed below).
3. **General** tab:
   - Name: `Travel Deal Agent`
   - User account: your own account (the default shown under "When running the task, use
     the following user account").
   - Select **Run only when user is logged on**.
   - Leave **Run with highest privileges** unchecked.
4. **Triggers** tab → **New…**:
   - Begin the task: **At log on**
   - Settings: **Specific user** -- your account.
   - **Enabled** checked; no repetition.
5. **Actions** tab → **New…**:
   - Action: **Start a program**
   - Program/script: `C:\path\to\travel-deal-agent\.venv\Scripts\pythonw.exe`
   - Add arguments (optional): `-m travel_deal_agent --watch`
   - Start in (optional): `C:\path\to\travel-deal-agent`

   **Start in is required in practice**, even though Windows labels it optional: without
   it, Python cannot find the `travel_deal_agent` package and exits immediately.
6. **Conditions** tab -- uncheck:
   - Start the task only if the computer is idle for
   - Start the task only if the computer is on AC power
   - Stop if the computer switches to battery power
   - Wake the computer to run this task
   - Start only if the following network connection is available
7. **Settings** tab:
   - **Allow task to be run on demand**: checked
   - **Run task as soon as possible after a scheduled start is missed**: unchecked
   - **If the task fails, restart every**: 1 minute; **Attempt to restart up to**: 3 times
   - **Stop the task if it runs longer than**: **unchecked** (Windows pre-selects 3 days,
     which would kill the agent)
   - **If the running task does not end when requested, force it to stop**: checked
   - **If the task is already running, then the following rule applies**:
     **Do not start a new instance** -- this is what prevents two agents from writing to
     the same database.
8. Click **OK**. With "Run only when user is logged on", no password is stored.

### Start it for the first time

In **Task Scheduler Library**, select **Travel Deal Agent** and click **Run** in the right
pane. The status changes to **Running** (press F5 to refresh). No window appears -- that is
expected with `pythonw.exe`. Wait a minute or two and [check the log](#check-that-the-agent-is-working).

From now on the agent starts automatically every time you sign in to Windows. It does not
run while the computer is asleep, shut down or signed out, and the task does not wake the
computer.

### Stop and start again

- **Stop:** Task Scheduler → Task Scheduler Library → select **Travel Deal Agent** →
  **End**. The status returns to **Ready**.
- **Start again:** select the task → **Run**.

### Restart after a code or configuration change

The running process reads its code, `config.json` and `.env` only at start-up. After you:

- update the code (for example `git pull`),
- edit `config.json`, or
- change `.env` / Telegram secrets or other environment variables,

restart the task: **End**, then **Run**. Reinstalling is not needed. Run
`.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt` again only if an update
changed `requirements.txt` or `requirements-dev.txt`.

### Active hours

The committed configuration scans only between **07:00 and 23:30 (Europe/Warsaw)**:

```json
"active_hours": {
  "enabled": true,
  "timezone": "Europe/Warsaw",
  "active_from": "07:00",
  "active_until": "23:30"
}
```

Outside this window the process stays alive but makes no requests; when the window opens
it runs one normal check of each due provider, not a burst of missed scans. `active_from`
is inclusive, `active_until` exclusive, windows may wrap past midnight, and
`"enabled": false` scans at any hour. `--force` bypasses the window for one manual run.

## Check that the agent is working

Logs are written to `logs\agent.log` in the project folder (created automatically, rotated
at about 2 MB with up to three backups, `agent.log.1`..`agent.log.3`). Timestamps are local
time. From the project folder:

```powershell
# Last 40 lines
Get-Content .\logs\agent.log -Tail 40

# Most recent start of the program
Select-String -Path .\logs\agent.log -Pattern "Notifications:" | Select-Object -Last 1

# Recent errors
Select-String -Path .\logs\agent.log -Pattern " ERROR " | Select-Object -Last 5
```

What normal operation looks like:

| Log line | Meaning |
| --- | --- |
| `Notifications: telegram` / `Notifications: console` | The program started; shows the delivery channel |
| `Search started` ... `Search finished in ...s; N unique matches` | One scheduler pass. In `--watch` mode passes repeat about every 30 s (`scheduler.idle_poll_seconds`), most of them with no provider due; `0 unique matches` is normal |
| `Checking provider tui` → `Provider tui fetched N offers` → `Provider tui matched N offers` | A real scan of one provider (same for `itaka` and `wakacje.pl`) |
| `Outside active hours; skipping provider scans` | Night-time idling, expected |
| `Provider ... failed; retry in ...s` | That source failed; it backs off and retries later, others continue |
| `Notification ... failed; will retry later` | Telegram delivery failed; the alert stays queued |

With Telegram configured, alerts go to the chat and do not appear in the log; with the
console fallback, the full alert text is logged.

**Task Scheduler:** the task's **Status** column shows **Running** while the agent runs;
**Last Run Result** `0x41301` means "currently running". `(0x2)` means the program exited
with a configuration or command-line error.

**Task Manager** (Details tab): a running agent normally shows **two** `pythonw.exe`
processes. The virtual environment's `pythonw.exe` is a small launcher that starts the real
interpreter as a child process; this is not a duplicate agent. After **End**, both should
disappear.

## Moving to another computer

Do **not** copy these from the old machine -- they are machine-specific or regenerated:

- `.venv\` (contains absolute paths to the old Python installation)
- `__pycache__\`, `.pytest_cache\`, `.mypy_cache\`, `.ruff_cache\`
- `logs\`
- diagnostic folders under `data\` (for example browser capture or recon artifacts)

Instead, on the new computer: clone the repository, create a new `.venv`, install the
dependencies and Chromium, create `.env` with the Telegram secrets (transfer them
privately, not through the repository), review `config.json` and create the Task Scheduler
task -- that is, follow [Running on a new Windows computer](#running-on-a-new-windows-computer)
and the Task Scheduler section above.

The price history lives in one SQLite database, `data\offers.sqlite3` (configurable with
`TDA_DATABASE`). Choose one of two options:

**A. New history.** Do not copy the database. The agent creates an empty one on first
start. Every currently matching offer is then treated as new, so expect a first wave of
`NOWA` alerts, and price-drop detection starts from zero.

**B. Keep price history.**

1. **Stop the agent on both computers** (Task Scheduler → End; confirm no `pythonw.exe`
   is left in Task Manager).
2. On the old computer, copy `data\offers.sqlite3`.
3. The application uses SQLite's default rollback journal, not WAL, so there are no
   `-wal`/`-shm` files and, once the agent is stopped, `offers.sqlite3` alone is the
   complete database. If an `offers.sqlite3-journal` file is present next to it, the last
   write was interrupted: do not delete it -- start and stop the agent once on the old
   computer so SQLite recovers the database, then copy `offers.sqlite3`.
4. On the new computer, place the file at `data\offers.sqlite3` in the project folder
   (create `data` if needed) **before** the agent's first start there.
5. Start the task on the new computer only. Never run both computers' agents with
   copies of the same history and the same Telegram chat at the same time, or you will
   receive duplicate alerts.

## Troubleshooting

- **Status stays Ready after Run, or returns to Ready quickly.** Check the task's
  Program/script and Start in paths. A `Last Run Result` of `(0x2)` means a configuration
  or command-line error. These errors happen **before** logging starts, so they are not in
  `agent.log` and `pythonw.exe` shows nothing. End the task, then run
  `.\.venv\Scripts\python.exe -m travel_deal_agent --watch` in a terminal to see the
  message, fix it, press Ctrl+C and start the task again.
- **`No module named travel_deal_agent`.** The Start in field is empty or points to the
  wrong folder.
- **TUI fails with a missing browser executable.** Run
  `.\.venv\Scripts\python.exe -m playwright install chromium` as the same Windows user that
  runs the task.
- **`Notifications: console` although Telegram is configured.** Both variables must be set
  in `.env` without a leading `#`, and the agent must be restarted.
- **The log is not changing.** Confirm the task is Running and `pythonw.exe` is in Task
  Manager. Outside active hours only short idle lines are written.
- **Project folder moved or `.venv` recreated.** Update Program/script and Start in in the
  task's Actions tab to the new paths.
