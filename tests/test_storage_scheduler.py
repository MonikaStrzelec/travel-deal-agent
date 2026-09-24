import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from travel_deal_agent.config import Settings, load_settings
from travel_deal_agent.models import Offer, duplicate_key
from travel_deal_agent.notifications import Notifier, deliver_pending
from travel_deal_agent.providers.mock import MockProvider
from travel_deal_agent.scheduler import Scheduler
from travel_deal_agent.storage import Notification, Store


class RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.sent: list[int] = []

    def send(self, notification: Notification) -> None:
        self.sent.append(notification["id"])


def test_new_offer_survives_restart(offer: Offer, store: Store, settings: Settings) -> None:
    store.observe(offer, True)

    with Store(settings.database) as reopened:
        events = reopened.observe(offer, True)
        snapshot = reopened.get_offer(offer.provider, offer.offer_id)

        assert events == []
        assert len(reopened.pending()) == 1
        assert snapshot is not None
        assert snapshot.found_at <= snapshot.last_seen
        assert snapshot.price_per_person == Decimal("1299")


def test_price_change_and_cumulative_drop(offer: Offer, store: Store) -> None:
    # Arrange / Act / Assert: any drop below the lowest alerted price alerts,
    # with no minimum drop amount; an unchanged or higher price never does.
    assert store.observe(offer, True) == ["new_offer"]
    small_drop = replace(offer, price_per_person=Decimal("1298"))
    assert store.observe(small_drop, True) == ["price_changed", "price_drop"]
    assert store.observe(small_drop, True) == []
    assert store.observe(offer, True) == ["price_changed"]
    assert store.observe(small_drop, True) == ["price_changed"]
    big_drop = replace(offer, price_per_person=Decimal("1199"))
    assert store.observe(big_drop, True) == ["price_changed", "price_drop"]
    assert [n["kind"] for n in store.pending()] == ["new_offer", "price_drop", "price_drop"]
    assert [n["previous_price"] for n in store.pending()] == [None, "1299", "1298"]
    assert store.price_history(offer.provider, offer.offer_id) == [
        Decimal("1299"),
        Decimal("1298"),
        Decimal("1299"),
        Decimal("1298"),
        Decimal("1199"),
    ]


def test_unchanged_offer_is_not_realerted_on_later_hourly_scans(
    offer: Offer, settings: Settings
) -> None:
    # Arrange: hourly scans of the same good offer, with the production re-arm window.
    now = [datetime(2026, 9, 24, 7, tzinfo=timezone.utc)]
    with Store(
        settings.database, alert_rearm_after=settings.alert_rearm_after, clock=lambda: now[0]
    ) as store:
        # Act
        events = []
        for _ in range(48):
            events.append(store.observe(offer, True))
            now[0] += timedelta(hours=1)
        # Assert
        assert events[0] == ["new_offer"]
        assert all(later == [] for later in events[1:])
        assert len(store.pending()) == 1


def test_offer_returning_after_the_rearm_window_alerts_again(
    offer: Offer, settings: Settings
) -> None:
    # Arrange
    now = [datetime(2026, 9, 24, 7, tzinfo=timezone.utc)]
    rearm = timedelta(hours=24)
    with Store(settings.database, alert_rearm_after=rearm, clock=lambda: now[0]) as store:
        store.observe(offer, True)
        # Act: the offer stops qualifying (still observed, but ineligible) for a while.
        now[0] += timedelta(hours=1)
        store.observe(offer, False)
        now[0] += rearm - timedelta(hours=1, seconds=1)
        early = store.observe(offer, True)
        now[0] += rearm
        returned = store.observe(offer, True)
        again = store.observe(offer, True)
        # Assert
        assert early == []
        assert returned == ["new_offer"]
        assert again == []
        assert [n["previous_price"] for n in store.pending()] == [None, None]


def test_alert_state_from_an_older_database_is_upgraded(offer: Offer, settings: Settings) -> None:
    # Arrange: the alert table as created before the re-arm column existed.
    connection = sqlite3.connect(settings.database)
    connection.execute(
        "CREATE TABLE alert_state (group_key TEXT PRIMARY KEY, lowest_alert_price TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO alert_state VALUES (?, ?)", (duplicate_key(offer), str(offer.price_per_person))
    )
    connection.commit()
    connection.close()
    # Act
    with Store(settings.database, alert_rearm_after=timedelta(hours=24)) as store:
        repeated = store.observe(offer, True)
        cheaper = store.observe(replace(offer, price_per_person=Decimal("1200")), True)
    # Assert: the previously alerted offer is not re-announced; a better price is.
    assert repeated == []
    assert cheaper == ["price_changed", "price_drop"]


def test_rearm_window_must_be_positive(settings: Settings) -> None:
    with pytest.raises(ValueError, match="re-arm"):
        Store(settings.database, alert_rearm_after=timedelta(0))


def test_first_eligible_and_cross_provider_alert(offer: Offer, store: Store) -> None:
    assert store.observe(offer, False) == []
    assert store.observe(offer, True) == ["new_offer"]
    assert store.observe(replace(offer, provider="other"), True) == []


def test_notification_retry(store: Store, offer: Offer) -> None:

    class FailingNotifier(Notifier):
        def send(self, notification: Notification) -> None:
            raise RuntimeError("Offline delivery failure")

    store.observe(offer, True)
    deliver_pending(store, FailingNotifier())
    assert len(store.pending()) == 1
    notifier = RecordingNotifier()
    deliver_pending(store, notifier)
    deliver_pending(store, notifier)
    assert len(notifier.sent) == 1
    assert store.pending() == []


def test_scheduler_intervals_and_restart(settings: Settings, store: Store) -> None:
    notifier = RecordingNotifier()
    # Scoped to the one provider this test wires up, with enabled explicitly
    # forced True -- independent of whatever the real config.json currently
    # has (it disables mock so Wakacje.pl can run alone).
    settings = replace(
        settings, providers={"mock": {**settings.providers["mock"], "enabled": True}}
    )
    scheduler = Scheduler(settings, [MockProvider()], store, notifier, clock=lambda: 1000)
    assert len(scheduler.run_once()) == 3
    assert len(notifier.sent) == 3
    assert scheduler.run_once() == []
    restarted = Scheduler(settings, [MockProvider()], store, notifier, clock=lambda: 1600)
    assert len(restarted.run_once()) == 3
    assert len(notifier.sent) == 3


def test_range_uses_injected_rng_deterministically(settings: Settings, store: Store) -> None:
    # Arrange: a successful cycle draws exactly once from the configured range.
    calls: list[tuple[float, float]] = []

    def rng(low: float, high: float) -> float:
        calls.append((low, high))
        return 777.0

    settings = replace(
        settings,
        providers={
            "mock": {
                "enabled": True,
                "interval_seconds": 600,
                "interval_min_seconds": 480,
                "interval_max_seconds": 900,
            }
        },
    )
    scheduler = Scheduler(
        settings, [MockProvider()], store, RecordingNotifier(), clock=lambda: 1000, random_range=rng
    )
    # Act
    scheduler.run_once()
    # Assert
    assert store.run_state("mock") == {"next_run": 1000 + 777, "failures": 0}
    assert calls == [(480, 900)]


def test_equal_range_bounds_give_the_exact_interval_without_drawing(
    settings: Settings, store: Store
) -> None:
    # Arrange: min == max must be exact and must not even call the RNG.
    def poisoned(low: float, high: float) -> float:
        raise AssertionError("must not randomize when interval_min == interval_max")

    settings = replace(
        settings,
        providers={
            "mock": {
                "enabled": True,
                "interval_seconds": 600,
                "interval_min_seconds": 700,
                "interval_max_seconds": 700,
            }
        },
    )
    scheduler = Scheduler(
        settings,
        [MockProvider()],
        store,
        RecordingNotifier(),
        clock=lambda: 1000,
        random_range=poisoned,
    )
    # Act
    scheduler.run_once()
    # Assert
    assert store.run_state("mock") == {"next_run": 1700, "failures": 0}


def test_missing_range_fields_preserve_interval_seconds_behavior(
    settings: Settings, store: Store
) -> None:
    # Arrange: no interval_min/max_seconds configured (the real mock provider config).
    def poisoned(low: float, high: float) -> float:
        raise AssertionError("interval_seconds fallback must not randomize")

    # Scoped to the one provider this test wires up, with enabled explicitly
    # forced True -- independent of whatever the real config.json currently
    # has (it disables mock so Wakacje.pl can run alone). Every other field,
    # in particular the absence of interval_min/max_seconds, is kept exactly
    # as the real "mock" provider config has it -- that's the point of this
    # test.
    settings = replace(
        settings, providers={"mock": {**settings.providers["mock"], "enabled": True}}
    )
    scheduler = Scheduler(
        settings,
        [MockProvider()],
        store,
        RecordingNotifier(),
        clock=lambda: 1000,
        random_range=poisoned,
    )
    # Act
    scheduler.run_once()
    # Assert
    assert store.run_state("mock") == {
        "next_run": 1000 + settings.providers["mock"]["interval_seconds"],
        "failures": 0,
    }


def test_restart_honors_persisted_next_run_without_rerandomizing(
    settings: Settings, store: Store
) -> None:
    # Arrange
    settings = replace(
        settings,
        providers={
            "mock": {
                "enabled": True,
                "interval_seconds": 600,
                "interval_min_seconds": 480,
                "interval_max_seconds": 900,
            }
        },
    )
    scheduler = Scheduler(
        settings,
        [MockProvider()],
        store,
        RecordingNotifier(),
        clock=lambda: 1000,
        random_range=lambda low, high: 500.0,
    )
    scheduler.run_once()
    persisted = store.run_state("mock")
    assert persisted == {"next_run": 1500, "failures": 0}

    def poisoned(low: float, high: float) -> float:
        raise AssertionError("a restart before next_run must not draw a new random value")

    # Act: a fresh Scheduler instance (simulating a restart) before the persisted due time.
    restarted = Scheduler(
        settings,
        [MockProvider()],
        store,
        RecordingNotifier(),
        clock=lambda: 1200,
        random_range=poisoned,
    )
    result = restarted.run_once()
    # Assert
    assert result == []
    assert store.run_state("mock") == persisted


def test_provider_failure_isolation_and_backoff(settings: Settings, store: Store) -> None:

    class BrokenProvider(MockProvider):
        name = "broken"

        def fetch(self) -> list[Offer]:
            raise RuntimeError("Fixture failure")

    settings = replace(
        settings,
        providers={
            "broken": {"enabled": True, "interval_seconds": 100},
            "mock": {"enabled": True, "interval_seconds": 600},
        },
    )
    scheduler = Scheduler(
        settings, [BrokenProvider(), MockProvider()], store, RecordingNotifier(), clock=lambda: 1000
    )
    assert len(scheduler.run_once()) == 3
    assert store.run_state("broken") == {"next_run": 1200, "failures": 1}
    assert store.run_state("mock") == {"next_run": 1600, "failures": 0}


def test_backoff_after_failure_ignores_configured_range(settings: Settings, store: Store) -> None:
    # Arrange: even with a range configured, a failed cycle keeps the exact deterministic backoff.
    class BrokenProvider(MockProvider):
        name = "broken"

        def fetch(self) -> list[Offer]:
            raise RuntimeError("Fixture failure")

    def poisoned(low: float, high: float) -> float:
        raise AssertionError("backoff must not randomize")

    settings = replace(
        settings,
        providers={
            "broken": {
                "enabled": True,
                "interval_seconds": 100,
                "interval_min_seconds": 480,
                "interval_max_seconds": 900,
            }
        },
    )
    scheduler = Scheduler(
        settings,
        [BrokenProvider()],
        store,
        RecordingNotifier(),
        clock=lambda: 1000,
        random_range=poisoned,
    )
    # Act
    assert scheduler.run_once() == []
    # Assert: unchanged formula, interval_seconds * 2**failures, not the configured range.
    assert store.run_state("broken") == {"next_run": 1200, "failures": 1}


def test_zero_offers_is_a_successful_run_not_a_failure(settings: Settings, store: Store) -> None:
    # Arrange
    class EmptyProvider(MockProvider):
        name = "empty"

        def fetch(self) -> list[Offer]:
            return []

    settings = replace(
        settings,
        providers={
            "empty": {
                "enabled": True,
                "interval_seconds": 100,
                "interval_min_seconds": 80,
                "interval_max_seconds": 120,
            }
        },
    )
    scheduler = Scheduler(
        settings,
        [EmptyProvider()],
        store,
        RecordingNotifier(),
        clock=lambda: 1000,
        random_range=lambda low, high: 90.0,
    )
    # Act
    result = scheduler.run_once()
    # Assert: an empty fetch is a success, not a failure, and gets the normal randomized cadence.
    assert result == []
    assert store.run_state("empty") == {"next_run": 1090, "failures": 0}


@pytest.mark.parametrize(
    "overrides",
    [
        {"interval_min_seconds": 0, "interval_max_seconds": 900},
        {"interval_min_seconds": 900, "interval_max_seconds": 480},
    ],
)
def test_invalid_interval_range_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, int]
) -> None:
    # Arrange
    from travel_deal_agent.config import ROOT

    raw = json.loads((ROOT / "config.json").read_text())
    raw["providers"]["itaka"].update(overrides)
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setenv("TDA_CONFIG", str(path))
    # Act / Assert
    with pytest.raises(ValueError):
        load_settings()


@pytest.mark.parametrize("field", ["interval_min_seconds", "interval_max_seconds"])
def test_partial_interval_range_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    # Arrange: only one end of the range is set, the other left absent.
    from travel_deal_agent.config import ROOT

    raw = json.loads((ROOT / "config.json").read_text())
    other = "interval_max_seconds" if field == "interval_min_seconds" else "interval_min_seconds"
    raw["providers"]["itaka"][field] = 600
    raw["providers"]["itaka"].pop(other, None)
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setenv("TDA_CONFIG", str(path))
    # Act / Assert
    with pytest.raises(ValueError, match="must be set together"):
        load_settings()


def test_unknown_enabled_provider(settings: Settings, store: Store) -> None:
    settings = replace(settings, providers={"rainbow": {"enabled": True, "interval_seconds": 600}})
    with pytest.raises(ValueError, match="No implementation"):
        Scheduler(settings, [], store, RecordingNotifier())


@pytest.mark.parametrize(
    "field,value", [("min_nights", 0), ("max_nights", 0), ("max_price", "NaN"), ("people", 0)]
)
def test_invalid_config(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str | int,
) -> None:
    from travel_deal_agent.config import ROOT

    raw = json.loads((ROOT / "config.json").read_text())
    raw["filters"][field] = value
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setenv("TDA_CONFIG", str(path))
    with pytest.raises(ValueError):
        load_settings()


def test_invalid_duration_range_rejected(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """min_nights > max_nights must be rejected, even though the production
    config currently leaves both null (no duration limit)."""
    from travel_deal_agent.config import ROOT

    raw = json.loads((ROOT / "config.json").read_text())
    raw["filters"]["min_nights"] = 10
    raw["filters"]["max_nights"] = 5
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setenv("TDA_CONFIG", str(path))
    with pytest.raises(ValueError, match="max_nights"):
        load_settings()
