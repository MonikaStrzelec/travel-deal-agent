"""run_forever must survive a failing cycle with backoff, yet still honour shutdown."""

import logging
from collections.abc import Iterator
from dataclasses import replace

import pytest

from travel_deal_agent.config import Settings
from travel_deal_agent.models import Offer
from travel_deal_agent.notifications import ConsoleNotifier
from travel_deal_agent.providers.mock import MockProvider
from travel_deal_agent.scheduler import Scheduler
from travel_deal_agent.storage import Store


def _scheduler(settings: Settings, store: Store, waits: list[float], stop_after: int) -> Scheduler:
    scoped = replace(
        settings,
        providers={"mock": {**settings.providers["mock"], "enabled": True}},
        scheduler={**settings.scheduler, "idle_poll_seconds": 10, "max_backoff_exponent": 2},
    )

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) >= stop_after:
            raise KeyboardInterrupt

    return Scheduler(
        scoped, [MockProvider()], store, ConsoleNotifier(), clock=lambda: 1000, sleep=sleep
    )


def test_failing_cycle_is_logged_backed_off_and_followed_by_another_cycle(
    settings: Settings,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange: the first cycle fails outside any provider, later ones succeed.
    waits: list[float] = []
    scheduler = _scheduler(settings, store, waits, stop_after=2)
    calls: list[int] = []
    real_run_once = scheduler.run_once

    def flaky(force: bool = False) -> list[Offer]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return real_run_once(force)

    monkeypatch.setattr(scheduler, "run_once", flaky)

    # Act
    with caplog.at_level(logging.ERROR), pytest.raises(KeyboardInterrupt):
        scheduler.run_forever()

    # Assert
    assert len(calls) == 2
    assert waits[0] == 10
    assert "Polling cycle failed" in caplog.text
    assert "database is locked" in caplog.text


def test_repeated_failures_back_off_exponentially_up_to_the_cap(
    settings: Settings, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []
    scheduler = _scheduler(settings, store, waits, stop_after=5)

    def always_fails(force: bool = False) -> list[Offer]:
        raise RuntimeError("disk full")

    monkeypatch.setattr(scheduler, "run_once", always_fails)

    with pytest.raises(KeyboardInterrupt):
        scheduler.run_forever()

    assert waits == [10, 20, 40, 40, 40]


def test_success_resets_the_failure_backoff(
    settings: Settings, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []
    scheduler = _scheduler(settings, store, waits, stop_after=3)
    outcomes: Iterator[Exception | list[Offer]] = iter(
        [RuntimeError("boom"), [], RuntimeError("boom")]
    )

    def scripted(force: bool = False) -> list[Offer]:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(scheduler, "run_once", scripted)

    with pytest.raises(KeyboardInterrupt):
        scheduler.run_forever()

    assert waits[0] == 10
    assert waits[2] == 10


@pytest.mark.parametrize("exit_signal", [KeyboardInterrupt, SystemExit])
def test_shutdown_signals_raised_by_a_cycle_are_not_swallowed(
    settings: Settings,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    exit_signal: type[BaseException],
) -> None:
    waits: list[float] = []
    scheduler = _scheduler(settings, store, waits, stop_after=5)

    def interrupted(force: bool = False) -> list[Offer]:
        raise exit_signal

    monkeypatch.setattr(scheduler, "run_once", interrupted)

    with pytest.raises(exit_signal):
        scheduler.run_forever()

    assert waits == []
