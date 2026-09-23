"""Sequential per-provider polling with persistent due times and failure backoff."""

import logging
import random
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime, timezone

from .active_hours import is_within_active_hours
from .config import Settings
from .config_types import ProviderConfig
from .models import Offer
from .notifications import Notifier, deliver_pending
from .pipeline import OfferPipeline
from .providers.base import Provider
from .providers.external_rating import ExternalHotelRatingProvider
from .storage import Store

logger = logging.getLogger(__name__)


class Scheduler:
    """Coordinate due sources; inject clocks and sleep to keep tests deterministic."""

    def __init__(
        self,
        settings: Settings,
        providers: Sequence[Provider],
        store: Store,
        notifier: Notifier,
        clock: Callable[[], float] = time.time,
        external_provider: ExternalHotelRatingProvider | None = None,
        today: Callable[[], date] = date.today,
        sleep: Callable[[float], None] = time.sleep,
        external_providers: Sequence[ExternalHotelRatingProvider] | None = None,
        random_range: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.settings = settings
        self.providers = {p.name: p for p in providers}
        if len(self.providers) != len(providers):
            raise ValueError("Provider names must be unique")
        self.store = store
        self.notifier = notifier
        self.clock = clock
        self.sleep = sleep
        self.random_range = random_range
        self.pipeline = OfferPipeline(settings, store, external_provider, today, external_providers)
        unknown = {
            name for name, cfg in settings.providers.items() if cfg["enabled"]
        } - self.providers.keys()
        if unknown:
            raise ValueError(f"No implementation for enabled providers: {sorted(unknown)}")

    def run_once(self, force: bool = False) -> list[Offer]:
        """Poll due sources and return ranked matches from this check only.

        Outside the configured active-hours window, provider scans are
        skipped entirely (force bypasses this, like it bypasses due times).
        Already-pending notifications still get a delivery attempt below.
        """
        started = time.monotonic()
        logger.info("Search started")
        accepted = []
        if force or self._within_active_hours():
            for name, config in self.settings.providers.items():
                if not config["enabled"]:
                    continue
                state = self.store.run_state(name)
                if not force and state and self.clock() < state["next_run"]:
                    continue
                offers = self._fetch(name, config)
                if offers is not None:
                    matches = self.pipeline.filter_batch(name, offers)
                    accepted.extend(matches)
                    logger.info("Provider %s matched %s offers", name, len(matches))
        else:
            logger.info("Outside active hours; skipping provider scans")
        result = self.pipeline.finalize(accepted)
        deliver_pending(self.store, self.notifier)
        logger.info(
            "Search finished in %.3fs; %s unique matches", time.monotonic() - started, len(result)
        )
        return result

    def _fetch(self, name: str, config: ProviderConfig) -> list[Offer] | None:
        started = time.monotonic()
        state = self.store.run_state(name)
        failures = state["failures"] if state else 0
        self.store.schedule(name, self.clock() + config["interval_seconds"], failures)
        try:
            logger.info("Checking provider %s", name)
            offers = self.providers[name].fetch()
        except Exception:
            # Adapter failures are isolated; database failures outside this boundary propagate.
            failures += 1
            exponent = min(failures, self.settings.scheduler["max_backoff_exponent"])
            delay = config["interval_seconds"] * 2**exponent
            logger.exception("Provider %s failed; retry in %ss", name, delay)
            self.store.schedule(name, self.clock() + delay, failures)
            return None
        finally:
            logger.info("Provider %s finished in %.3fs", name, time.monotonic() - started)
        logger.info("Provider %s fetched %s offers", name, len(offers))
        self.store.schedule(name, self.clock() + self._next_delay(config), 0)
        return offers

    def _within_active_hours(self) -> bool:
        """Convert the injected epoch clock (UTC) into the configured local window."""
        moment = datetime.fromtimestamp(self.clock(), tz=timezone.utc)
        return is_within_active_hours(self.settings.active_hours, moment)

    def _next_delay(self, config: ProviderConfig) -> float:
        """Randomize only the steady-state cadence; backoff stays deterministic."""
        minimum = config.get("interval_min_seconds")
        maximum = config.get("interval_max_seconds")
        if minimum is None or maximum is None:
            return config["interval_seconds"]
        return float(minimum) if minimum == maximum else self.random_range(minimum, maximum)

    def run_forever(self) -> None:
        """Poll continuously until interrupted; due times survive restarts."""
        while True:
            self.run_once()
            idle = self.settings.scheduler["idle_poll_seconds"]
            if self._within_active_hours():
                due = []
                for name, config in self.settings.providers.items():
                    state = self.store.run_state(name)
                    if config["enabled"] and state is not None:
                        due.append(state["next_run"])
                wait = min(idle, min(due) - self.clock()) if due else idle
            else:
                # Providers were never fetched this cycle, so their due times
                # are frozen (not necessarily in the future); a plain idle
                # poll avoids both a busy loop and any dependency on those
                # stale due times. The next active-hours entry is picked up
                # by the run_once() gate above, at worst one idle interval late.
                wait = idle
            self.sleep(max(1, wait))
