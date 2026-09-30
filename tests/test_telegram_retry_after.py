"""Telegram HTTP 429: `retry_after` delays the outbox retry; no network access."""

import io
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

import pytest

from travel_deal_agent.config import Settings
from travel_deal_agent.models import Offer
from travel_deal_agent.notifications import (
    Notifier,
    TelegramDeliveryError,
    UrllibTelegramTransport,
    deliver_pending,
)
from travel_deal_agent.providers.mock import MockProvider
from travel_deal_agent.storage import Notification, NotificationRetryPolicy, Store

START = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


class RateLimitedNotifier(Notifier):
    def __init__(self, retry_after: float | None) -> None:
        self.retry_after = retry_after
        self.attempted: list[int] = []

    def send(self, notification: Notification) -> None:
        self.attempted.append(notification["id"])
        raise TelegramDeliveryError("Telegram API returned HTTP 429", self.retry_after)


@pytest.fixture
def raise_http_error(monkeypatch: pytest.MonkeyPatch) -> Callable[[int, dict[str, object]], None]:
    def install(code: int, body: dict[str, object]) -> None:
        def fake_urlopen(request: object, timeout: float | None = None) -> None:
            payload = io.BytesIO(json.dumps(body).encode())
            raise HTTPError("https://api.telegram.org/hidden", code, "err", {}, payload)  # type: ignore[arg-type]

        monkeypatch.setattr("travel_deal_agent.notifications.urlopen", fake_urlopen)

    return install


def _send_expecting_error() -> TelegramDeliveryError:
    with pytest.raises(TelegramDeliveryError) as exc_info:
        UrllibTelegramTransport("1:token").send_message("1", "hi", 1.0)
    return exc_info.value


def test_transport_reads_retry_after_from_a_429_body(
    raise_http_error: Callable[[int, dict[str, object]], None],
) -> None:
    raise_http_error(
        429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 17}, "description": "x"}
    )

    assert _send_expecting_error().retry_after == 17


@pytest.mark.parametrize(
    "body",
    [
        {"ok": False},
        {"parameters": {}},
        {"parameters": {"retry_after": "soon"}},
        {"parameters": {"retry_after": -1}},
    ],
)
def test_transport_ignores_missing_or_malformed_retry_after(
    body: dict[str, object], raise_http_error: Callable[[int, dict[str, object]], None]
) -> None:
    raise_http_error(429, body)

    assert _send_expecting_error().retry_after is None


def test_retry_after_is_only_honoured_for_http_429(
    raise_http_error: Callable[[int, dict[str, object]], None],
) -> None:
    raise_http_error(400, {"parameters": {"retry_after": 30}})

    assert _send_expecting_error().retry_after is None


def _two_offers() -> tuple[Offer, Offer]:
    offers = MockProvider().fetch()
    return offers[0], offers[1]


def test_retry_after_postpones_the_first_retry_and_pauses_the_batch(settings: Settings) -> None:
    # Arrange: two pending alerts; a plain first failure would retry immediately.
    now = [START]
    with Store(settings.database, clock=lambda: now[0]) as store:
        first, second = _two_offers()
        store.observe(first, True)
        store.observe(second, True)
        notifier = RateLimitedNotifier(retry_after=120)

        # Act
        deliver_pending(store, notifier)

        # Assert: only one send was attempted, nothing is due until retry_after passed.
        assert len(notifier.attempted) == 1
        assert [n["id"] for n in store.pending()] == [2]
        now[0] += timedelta(seconds=119)
        assert [n["id"] for n in store.pending()] == [2]
        now[0] += timedelta(seconds=2)
        assert [n["id"] for n in store.pending()] == [1, 2]


def test_retry_after_still_counts_against_the_retry_budget(
    offer: Offer, settings: Settings
) -> None:
    policy = NotificationRetryPolicy(max_attempts=2)
    now = [START]
    with Store(settings.database, clock=lambda: now[0], notification_retry_policy=policy) as store:
        store.observe(offer, True)
        notifier = RateLimitedNotifier(retry_after=10)

        for _ in range(2):
            deliver_pending(store, notifier)
            now[0] += timedelta(seconds=11)

        assert store.pending() == []
        assert len(notifier.attempted) == 2


def test_retry_after_is_capped_at_the_policy_max_delay(offer: Offer, settings: Settings) -> None:
    policy = NotificationRetryPolicy(max_delay=timedelta(minutes=10))
    now = [START]
    with Store(settings.database, clock=lambda: now[0], notification_retry_policy=policy) as store:
        store.observe(offer, True)

        deliver_pending(store, RateLimitedNotifier(retry_after=86_400))

        now[0] += timedelta(minutes=11)
        assert len(store.pending()) == 1


def test_failure_without_retry_after_keeps_the_existing_immediate_retry(
    offer: Offer, settings: Settings
) -> None:
    with Store(settings.database, clock=lambda: START) as store:
        store.observe(offer, True)

        deliver_pending(store, RateLimitedNotifier(retry_after=None))

        assert len(store.pending()) == 1
