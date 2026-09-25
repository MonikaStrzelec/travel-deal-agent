"""Configurable local active-hours window: gates provider scans, not the whole scheduler.

Provider scans must be skipped outside the configured window, the scheduler must
keep waiting without a busy loop, resume normally once the window reopens, and
never send a synthetic "sleeping" notification. The `settings` fixture's
tests/fixtures/test_config.json disables the gate (`active_hours.enabled: false`)
so every other test file is unaffected; tests here opt back in explicitly.
"""

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import WriteConfig
from travel_deal_agent.active_hours import (
    is_within_active_hours,
    parse_time_of_day,
    validate_active_hours,
)
from travel_deal_agent.config import Settings, load_settings
from travel_deal_agent.config_types import ActiveHoursConfig
from travel_deal_agent.models import Offer
from travel_deal_agent.notifications import Notifier
from travel_deal_agent.providers.mock import MockProvider
from travel_deal_agent.scheduler import Scheduler
from travel_deal_agent.storage import Notification, Store

WARSAW = ZoneInfo("Europe/Warsaw")

DEFAULT_HOURS: ActiveHoursConfig = {
    "enabled": True,
    "timezone": "Europe/Warsaw",
    "active_from": "07:00",
    "active_until": "23:30",
}


def warsaw(iso: str) -> datetime:
    """Build a tz-aware Warsaw-local moment, e.g. warsaw("2024-01-10T06:59")."""
    return datetime.fromisoformat(iso).replace(tzinfo=WARSAW)


def clock_at(iso: str) -> Callable[[], float]:
    """A fixed clock callable for the given Warsaw-local moment."""
    return lambda: warsaw(iso).timestamp()


class RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> None:
        self.sent.append(notification)


# --- Pure boundary behavior (travel_deal_agent.active_hours) ---------------


@pytest.mark.parametrize(
    "moment,expected",
    [
        pytest.param("2024-01-10T12:00", True, id="within"),
        pytest.param("2024-01-10T06:59", False, id="just_before_active_from"),
        # active_from is inclusive.
        pytest.param("2024-01-10T07:00", True, id="exactly_active_from"),
        # active_until is exclusive, matching this project's price-band convention.
        pytest.param("2024-01-10T23:30", False, id="exactly_active_until"),
        pytest.param("2024-01-10T23:31", False, id="just_after_active_until"),
    ],
)
def test_active_hours_window_boundaries(moment: str, expected: bool) -> None:
    assert is_within_active_hours(DEFAULT_HOURS, warsaw(moment)) is expected


@pytest.mark.parametrize(
    "moment,expected",
    [
        ("2024-01-10T21:59", False),  # just before active_from (22:00)
        ("2024-01-10T22:00", True),  # exactly active_from
        ("2024-01-10T23:00", True),  # evening, before midnight
        ("2024-01-11T00:00", True),  # midnight, still inside the wrap
        ("2024-01-11T02:00", True),  # after midnight, before active_until
        ("2024-01-11T05:59", True),
        ("2024-01-11T06:00", False),  # exactly active_until
        ("2024-01-11T12:00", False),  # daytime, well outside an overnight window
    ],
)
def test_wraparound_window_across_midnight(moment: str, expected: bool) -> None:
    overnight: ActiveHoursConfig = {
        **DEFAULT_HOURS,
        "active_from": "22:00",
        "active_until": "06:00",
    }
    assert is_within_active_hours(overnight, warsaw(moment)) is expected


def test_disabled_gate_is_always_active_regardless_of_time() -> None:
    disabled: ActiveHoursConfig = {**DEFAULT_HOURS, "enabled": False}
    assert is_within_active_hours(disabled, warsaw("2024-01-10T03:00")) is True


def test_timezone_conversion_is_not_naive_utc() -> None:
    # 06:30 UTC is 07:30 local in Warsaw during winter (CET, UTC+1) -- inside
    # the 07:00-23:30 window -- but would be outside it if this instant were
    # (incorrectly) compared as if 06:30 were already the local time.
    utc_moment = datetime(2024, 1, 10, 6, 30, tzinfo=ZoneInfo("UTC"))
    assert is_within_active_hours(DEFAULT_HOURS, utc_moment) is True


def test_dst_summer_offset_is_handled_by_zoneinfo() -> None:
    # Warsaw is UTC+2 (CEST) in summer: 04:30 UTC is 06:30 local (still
    # before active_from), while 05:00 UTC is exactly 07:00 local.
    before = datetime(2024, 7, 10, 4, 30, tzinfo=ZoneInfo("UTC"))
    at_start = datetime(2024, 7, 10, 5, 0, tzinfo=ZoneInfo("UTC"))
    assert is_within_active_hours(DEFAULT_HOURS, before) is False
    assert is_within_active_hours(DEFAULT_HOURS, at_start) is True


def test_parse_time_of_day_rejects_malformed_values() -> None:
    with pytest.raises(ValueError):
        parse_time_of_day("0700")
    with pytest.raises(ValueError):
        parse_time_of_day("25:00")
    with pytest.raises(ValueError):
        parse_time_of_day("07:60")


def test_validate_active_hours_rejects_unknown_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        validate_active_hours({**DEFAULT_HOURS, "timezone": "Not/AZone"})


# --- Startup validation via load_settings() ---------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        pytest.param("timezone", "Not/AZone", id="unknown_timezone"),
        pytest.param("active_from", "7am", id="malformed_time"),
    ],
)
def test_load_settings_rejects_invalid_active_hours(
    write_config: WriteConfig, field: str, value: str
) -> None:
    write_config(lambda raw: raw["active_hours"].update({field: value}))
    with pytest.raises(ValueError):
        load_settings()


def test_production_config_active_hours_defaults(settings: Settings) -> None:
    # The settings fixture loads tests/fixtures/test_config.json, which
    # deliberately disables the gate; this checks the real config.json's
    # documented defaults independently, without touching the fixture.
    from travel_deal_agent.config import ROOT

    raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    assert raw["active_hours"] == {
        "enabled": True,
        "timezone": "Europe/Warsaw",
        "active_from": "07:00",
        "active_until": "23:30",
    }


# --- Scheduler wiring: run_once() gate --------------------------------------


def _with_mock_enabled(settings: Settings, active_hours: ActiveHoursConfig) -> Settings:
    return replace(
        settings,
        active_hours=active_hours,
        providers={"mock": {**settings.providers["mock"], "enabled": True}},
    )


def test_run_once_skips_provider_scans_outside_active_hours(
    settings: Settings, store: Store
) -> None:
    active_settings = _with_mock_enabled(settings, DEFAULT_HOURS)
    notifier = RecordingNotifier()
    scheduler = Scheduler(
        active_settings,
        [MockProvider()],
        store,
        notifier,
        clock=clock_at("2024-01-10T03:00"),
    )

    result = scheduler.run_once()

    assert result == []
    assert store.run_state("mock") is None
    # No fabricated "agent is sleeping" (or any other) message is ever sent.
    assert notifier.sent == []


def test_run_once_scans_normally_inside_active_hours(settings: Settings, store: Store) -> None:
    active_settings = _with_mock_enabled(settings, DEFAULT_HOURS)
    scheduler = Scheduler(
        active_settings,
        [MockProvider()],
        store,
        RecordingNotifier(),
        clock=clock_at("2024-01-10T12:00"),
    )

    result = scheduler.run_once()

    assert len(result) == 3
    assert store.run_state("mock") is not None


def test_force_bypasses_the_active_hours_gate(settings: Settings, store: Store) -> None:
    active_settings = _with_mock_enabled(settings, DEFAULT_HOURS)
    scheduler = Scheduler(
        active_settings,
        [MockProvider()],
        store,
        RecordingNotifier(),
        clock=clock_at("2024-01-10T03:00"),
    )

    result = scheduler.run_once(force=True)

    assert len(result) == 3


def test_resumes_normally_after_the_night_without_a_catch_up_burst(
    settings: Settings, store: Store
) -> None:
    class CountingMockProvider(MockProvider):
        def __init__(self, today: Callable[[], date]) -> None:
            super().__init__(today=today)
            self.calls = 0

        def fetch(self) -> list[Offer]:
            self.calls += 1
            return super().fetch()

    active_settings = _with_mock_enabled(settings, DEFAULT_HOURS)
    active_settings = replace(
        active_settings,
        providers={"mock": {**active_settings.providers["mock"], "interval_seconds": 3600}},
    )
    provider = CountingMockProvider(today=lambda: date(2024, 1, 9))
    notifier = RecordingNotifier()

    # A successful evening scan leaves next_run in the past once the night passes.
    scheduler = Scheduler(
        active_settings,
        [provider],
        store,
        notifier,
        clock=clock_at("2024-01-09T23:00"),
        today=lambda: date(2024, 1, 9),
    )
    scheduler.run_once()
    assert provider.calls == 1
    stale_next_run = store.run_state("mock")
    assert stale_next_run is not None

    # Every check overnight must skip the provider entirely.
    for moment in ("2024-01-10T00:30", "2024-01-10T03:00", "2024-01-10T06:59"):
        night_scheduler = Scheduler(
            active_settings,
            [provider],
            store,
            notifier,
            clock=clock_at(moment),
            today=lambda: date(2024, 1, 10),
        )
        assert night_scheduler.run_once() == []
    assert provider.calls == 1
    assert store.run_state("mock") == stale_next_run

    # Active hours resume: exactly one scan happens, not a burst of the
    # several intervals that were skipped overnight.
    morning_scheduler = Scheduler(
        active_settings,
        [provider],
        store,
        notifier,
        clock=clock_at("2024-01-10T07:00"),
        today=lambda: date(2024, 1, 10),
    )
    result = morning_scheduler.run_once()
    fresh_next_run = store.run_state("mock")

    assert len(result) == 3
    assert provider.calls == 2
    assert fresh_next_run is not None
    assert fresh_next_run["next_run"] > stale_next_run["next_run"]


# --- Scheduler wiring: run_forever() must not busy-loop ---------------------


def test_run_forever_polls_idly_outside_active_hours_then_resumes(
    settings: Settings, store: Store
) -> None:
    active_settings = _with_mock_enabled(settings, DEFAULT_HOURS)
    active_settings = replace(
        active_settings, scheduler={**active_settings.scheduler, "idle_poll_seconds": 3600}
    )
    state = {"now": warsaw("2024-01-10T05:00").timestamp()}

    def clock() -> float:
        return state["now"]

    waits: list[float] = []

    def fast_forward_sleep(seconds: float) -> None:
        waits.append(seconds)
        state["now"] += seconds
        if len(waits) >= 3:
            raise KeyboardInterrupt

    scheduler = Scheduler(
        active_settings,
        [MockProvider(today=lambda: date(2024, 1, 10))],
        store,
        RecordingNotifier(),
        clock=clock,
        sleep=fast_forward_sleep,
        today=lambda: date(2024, 1, 10),
    )

    with pytest.raises(KeyboardInterrupt):
        scheduler.run_forever()

    # 05:00 -> 06:00 -> 07:00: two inactive-hours idle polls at the full
    # configured interval -- never a near-zero wait from stale due times,
    # which is what a busy loop would look like.
    assert waits[0] == 3600
    assert waits[1] == 3600
    # By the third iteration (07:00, active) the provider has actually run.
    assert store.run_state("mock") is not None
