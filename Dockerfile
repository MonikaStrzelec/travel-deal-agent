# Base image pins the exact Chromium build matching requirements.txt's
# playwright==1.63.0 pin, and already has every OS-level dependency
# Chromium needs preinstalled -- no `playwright install` step is required
# and no browser download happens at container start.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app

# Runtime dependencies only (no pytest/ruff/mypy in the image).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application source and the default business configuration. Secrets
# (.env), data/ and logs/ are never copied -- see .dockerignore.
COPY travel_deal_agent ./travel_deal_agent
COPY config.json ./config.json

# Persistent SQLite database and rotating logs live here; docker-compose.yml
# bind-mounts host directories over these paths so a container restart or
# recreate never loses history.
RUN mkdir -p data logs && chown -R pwuser:pwuser /app

# The base image's built-in unprivileged "pwuser" already has the
# permissions Chromium's sandbox needs. Providers never pass --no-sandbox
# (see travel_deal_agent/providers/tui_browser.py and rainbow_browser.py),
# so running as root here would break the Chromium sandbox; running as
# pwuser keeps it working without touching provider code.
USER pwuser

# `--watch` is the existing continuous-scheduler mode (Scheduler.run_forever):
# it polls each enabled provider on its own persisted due time and sleeps
# between cycles, which is exactly the intended 24/7 entry point.
CMD ["python", "-m", "travel_deal_agent", "--watch"]
