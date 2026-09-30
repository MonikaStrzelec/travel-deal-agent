# Deployment (Docker / VPS)

This is the container setup for running the agent 24/7 on a Linux host (a VPS or any
Linux machine with Docker). It is the intended setup for continuous operation; the
Windows Task Scheduler setup ([docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md)) remains
available for local/development use; the README compares the two options.

> **Warning: run only one agent.** Once the VPS is running the agent, disable (or delete) the
> Windows Task Scheduler task and stop any local `--watch` process. Two agents (each with its
> own database) mean double scans, duplicate Telegram messages and double traffic to the
> travel sources.

The steps below (1-3) describe the environment this project has actually been prepared
and verified on: an Oracle Cloud Compute instance. If you already have a Linux host with
Docker, skip to [Prepare the project on the VPS](#4-prepare-the-project-on-the-vps).

## Files

- `Dockerfile` — builds the app on top of the official Playwright Python image
  (`mcr.microsoft.com/playwright/python`), pinned to the exact Chromium build that
  matches `requirements.txt`'s `playwright` version. That base image is required
  because the **TUI** provider drives headless Chromium; it is not optional weight.
  Runs as the base image's existing unprivileged `pwuser`, never root.
- `docker-compose.yml` — one service, `restart: unless-stopped`, secrets from `.env`
  (never baked into the image), bind mounts for persistent data and for the two files
  you may want to change without rebuilding (`config.json`, `hotel_watchlist.json`).
- `.dockerignore` — keeps `.env`, `data/`, `logs/`, caches, tests and personal files
  (`hotel_watchlist.json`) out of the build context and the image.
- `hotel_watchlist.example.json` — a commitable template with placeholder data,
  showing the expected shape of `hotel_watchlist.json` (see step 4 below).

## 1. Provision the VPS

This setup was prepared and verified on **Oracle Cloud Infrastructure (OCI)**:

- Region: Germany Central (Frankfurt)
- Shape: `VM.Standard.A1.Flex` (Arm/Ampere) — 1 OCPU, 6 GB RAM
- Image: Ubuntu 24.04 Minimal, aarch64 (ARM64)
- A dedicated VCN with a public subnet and a public IPv4 address
- SSH login user: `ubuntu`

This shape and image were configured under Oracle's Always Free / Free Tier offering.
Free-tier names, eligibility and limits are set by Oracle and can change — check the
current terms and available shapes in the OCI console before creating resources; this
document does not guarantee any tier or price.

Any similarly sized Linux host with Docker works the same way; see
[Minimal free VPS requirements](#minimal-free-vps-requirements) below for sizing.

## 2. Connect over SSH

```
ssh ubuntu@<VPS_IP>
```

Replace `<VPS_IP>` with the instance's public IPv4 address (never commit the real IP to
the repository) and point `-i` at your own private key file if it is not already loaded
in your SSH agent or client config. The private key itself must never be copied into the
project or committed.

## 3. Install Docker Engine and the Compose plugin

On Ubuntu 24.04, install from Docker's official APT repository (not the distro-bundled
`docker.io` package), so the Compose plugin (`docker compose`, v2) is included:

```
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update

sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Verify the install:

```
docker --version
docker compose version
sudo docker run --rm hello-world
```

This installs Docker as a systemd service that starts automatically on boot (see
[After a VPS reboot](#11-after-a-vps-reboot)). Optionally add your user to the `docker`
group (`sudo usermod -aG docker $USER`, then log out and back in) to run `docker`/
`docker compose` without `sudo`; the commands below assume this, otherwise prefix them
with `sudo`.

## 4. Prepare the project on the VPS

Copy the project to the VPS — git clone the repository, or copy the working tree some
other way — so that `Dockerfile`, `docker-compose.yml`, `config.json`, `.env.example` and
`hotel_watchlist.example.json` are present. Create the `data/` and `logs/` directories
yourself and give them to the container user (uid 1000, the image's `pwuser`) — if Docker
creates them they are owned by root and the agent cannot write its database or logs:

```
mkdir -p data logs && sudo chown 1000:1000 data logs
```

## 5. Create `.env` from the example

```
cp .env.example .env
```

Fill in the real values:

- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — omit both to fall back to local
  console-only logging instead of Telegram.
- `GOOGLE_PLACES_API_KEY` — omit to skip Google rating verification (no crash).
- `TDA_DATABASE`, `TDA_LOG_LEVEL`, `TDA_CONFIG` — optional overrides; the defaults
  already match this compose setup.

`.env` must never be committed and is never copied into the image (see
[Security](#security) below).

## 6. Create `hotel_watchlist.json` from the example

Make sure `config.json` and, if wanted, `hotel_watchlist.json` exist next to
`docker-compose.yml` on the VPS. `hotel_watchlist.json` is optional — its absence just
means no watched hotel — but if you want one, create it from the template and fill in
the real hotel:

```
cp hotel_watchlist.example.json hotel_watchlist.json
```

Then edit the copy: real hotel name/aliases/country/budget/airports, and, if you want
the dedicated Wakacje.pl per-hotel fetch, a real `provider_listings.wakacje.pl` URL.
That URL is never derived or guessed by this project (see `providers/wakacje.py`) —
find it once by searching the hotel's name on wakacje.pl yourself and copying the
resulting hotel page URL; it must be a `https://www.wakacje.pl/...` URL or config
loading rejects it. The placeholder URL in the example file is not a real listing.

## 7. Build and start

```
docker compose build
docker compose up -d
```

`up -d` starts exactly one container for the one service defined in
`docker-compose.yml` (no replicas configured) and will not accidentally start a second
worker; running `up -d` again just reconciles the existing container.

## 8. Check status and logs

```
docker compose ps
docker compose logs -f
```

- `ps` shows the service's current state (e.g. `Up`, `Restarting`).
- `logs -f` follows the same structured log lines as local runs: cycle start,
  per-provider fetch/result/error, retries and alerts. Secrets are never logged.
  `Ctrl+C` stops following the logs without stopping the container.

## 9. Stop, start and restart

```
docker compose stop      # stop the container, keep it (and volumes) defined
docker compose start     # start it again
docker compose restart   # stop, then start — e.g. after editing .env
docker compose down      # stop and remove the container (data/ and logs/ on the host are untouched)
```

Use `stop` / `start` / `restart` for routine operation. Use `down`, followed by
`up -d`, when the compose file or image itself changed (see next step).

## 10. Updating the application later

```
git pull
docker compose build
docker compose up -d
```

`data/offers.sqlite3` and `logs/` are bind-mounted from the host, not baked into the
image, so they survive rebuilds, recreation and updates unchanged.

## 11. After a VPS reboot

`restart: unless-stopped` (in `docker-compose.yml`) means Docker restarts the container
automatically whenever the Docker daemon starts — including after the VPS reboots —
*unless* the container had been explicitly stopped (`docker compose stop` or
`down`) before the reboot, in which case it stays stopped until you `start`/`up -d` it
again. Installing Docker from the official repository (step 3) already enables the
`docker` systemd service to start on boot, so no extra host-level unit is needed.

To confirm everything came back up correctly after a reboot:

```
ssh ubuntu@<VPS_IP>
docker compose ps
docker compose logs --tail=50
```

## Changing the watched hotel or business rules

Edit `config.json` and/or `hotel_watchlist.json` on the host, then
`docker compose up -d` (or `restart`) to apply — no rebuild needed, since both files
are bind-mounted read-only rather than baked into the image.

## Persistence

`./data` and `./logs` on the host are bind-mounted to `/app/data` and `/app/logs` in the
container, so the SQLite database (offers, price history, alert state, the
notification outbox and the Google rating cache all live in the one file at
`data/offers.sqlite3`) and the rotating log files survive `docker compose down`,
container recreation, image rebuilds and host reboots.

## Timezone

Business logic (active hours) already reads `active_hours.timezone` from `config.json`
via `zoneinfo`, independent of the container's system clock — this was true before this
setup and needed no change. `docker-compose.yml` sets `TZ=Europe/Warsaw` only so log
timestamps read in local time instead of UTC; it has no effect on scheduling decisions.

## Graceful shutdown

`docker stop` sends `SIGTERM`. `travel_deal_agent/__main__.py` already converts it into
the same `KeyboardInterrupt` path used for Ctrl+C locally, so the scheduler stops after
its current step and the SQLite connection is closed cleanly in a `finally` block — no
code change was needed for this.

## Healthcheck

Deliberately not configured. The app has no HTTP endpoint, and it would take a
disproportionate amount of new code (an HTTP server, or a synthetic file/DB-freshness
probe) to build one for an MVP that one person watches with `docker compose logs -f`.
`restart: unless-stopped` already recovers from a crashed process; that is enough here.
A polling cycle that fails (for example a locked database) is logged and retried with
backoff inside the process, so a persistent fault does not cause a fast restart loop.

## Inbound ports

None are required or exposed. The agent only makes outbound HTTPS requests (to the
travel sources, Google Places and the Telegram Bot API) and never listens for inbound
connections, so no port needs to be opened or forwarded on the VPS or its firewall.

## Security

- **`.env` is never committed.** It holds the Telegram bot token/chat ID and the
  optional Google Places API key — real secrets. It is excluded via `.gitignore` and
  `.dockerignore`; only `.env.example` (placeholders/comments, no real values) is
  committed. Never paste a real token into `config.json`, a commit message, an issue or
  a screenshot.
- **`hotel_watchlist.json` is local configuration, not a secret file, but it is still
  excluded from the repository** (`.gitignore`/`.dockerignore`) because it can contain
  personal travel preferences (destination, budget, airports). Only
  `hotel_watchlist.example.json`, with placeholder data, is committed.
- **The SSH private key used to reach the VPS never goes in the repository or in this
  project's files.** Keep it wherever your local SSH client/agent already expects keys.
- **Telegram bot token and the Google Places API key are secrets** — treat them like
  passwords: only in `.env` on the VPS, never logged, never in `config.json`.
- Example/template files (`.env.example`, `hotel_watchlist.example.json`) may be
  committed **only** as long as they keep placeholder values, never real credentials or
  a real watched hotel/location.

## Minimal free VPS requirements

- **Architecture:** this setup has been installed and run on `linux/arm64` (Ubuntu
  24.04 aarch64 on Oracle's `VM.Standard.A1.Flex`); the pinned Playwright base image
  also publishes a `linux/amd64` manifest, so an x86_64 host works too. Pull and boot
  the image once on the actual target architecture before relying on it, since Chromium
  sandbox behavior can be more sensitive on some ARM hosts.
- **OS:** any recent Linux distribution able to run Docker Engine (the app itself never
  touches the host OS beyond that).
- **RAM:** headless Chromium (used only by the TUI provider) is the main consumer;
  budget at least 1 GB, 2 GB if comfortably available, plus normal OS/Docker overhead.
- **CPU:** 1 vCPU is enough — this is a low-frequency poller (intervals are minutes to
  hours), not a compute-bound workload.
- **Disk:** the built image is ~3.7 GB (dominated by the Chromium/Playwright base
  image); SQLite and rotating logs stay small (the log handler caps itself at
  2 MB × 3 backups, and offer/price-history rows are tiny). 10 GB+ free disk is a
  comfortable minimum once you include the Docker engine itself and image layers.
- **Inbound ports:** none, see above.
