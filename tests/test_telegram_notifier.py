"""Telegram notifier: HTTP mocked, no network access, no token leakage."""

import io
import json
import logging
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from travel_deal_agent import config as config_module
from travel_deal_agent.config import TelegramConfig, load_settings, load_telegram_config
from travel_deal_agent.models import Offer
from travel_deal_agent.notifications import (
    TelegramDeliveryError,
    TelegramNotifier,
    UrllibTelegramTransport,
    deliver_pending,
)
from travel_deal_agent.storage import Store

SECRET_TOKEN = "123456:AAsuper-secret-token"


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []

    def send_message(self, chat_id: str, text: str, timeout: float) -> None:
        self.calls.append((chat_id, text, timeout))


class FailingTransport:
    def send_message(self, chat_id: str, text: str, timeout: float) -> None:
        raise TelegramDeliveryError("Telegram request failed: connection refused")


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_load_telegram_config_returns_none_when_unset() -> None:
    assert load_telegram_config({}) is None


def test_load_telegram_config_requires_both_variables_together() -> None:
    with pytest.raises(ValueError):
        load_telegram_config({"TELEGRAM_BOT_TOKEN": SECRET_TOKEN})
    with pytest.raises(ValueError):
        load_telegram_config({"TELEGRAM_CHAT_ID": "123"})


def test_load_telegram_config_reads_both_variables() -> None:
    config = load_telegram_config({"TELEGRAM_BOT_TOKEN": SECRET_TOKEN, "TELEGRAM_CHAT_ID": "123"})
    assert config == TelegramConfig(SECRET_TOKEN, "123")


def test_env_file_is_loaded_before_telegram_config_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors __main__'s call order: load_settings() must load .env before
    load_telegram_config() reads os.environ, or real .env credentials would be
    invisible to the Telegram notifier at startup."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    (tmp_path / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=env-file-token\nTELEGRAM_CHAT_ID=env-file-chat\n", encoding="utf-8"
    )
    monkeypatch.setenv("TDA_CONFIG", str(config_module.ROOT / "config.json"))
    monkeypatch.setenv("TDA_DATABASE", str(tmp_path / "offers.sqlite3"))
    monkeypatch.setattr(config_module, "ROOT", tmp_path)

    # Before load_settings() runs, the .env file has not been read yet.
    assert load_telegram_config() is None

    load_settings()

    assert load_telegram_config() == TelegramConfig("env-file-token", "env-file-chat")


def test_notifier_rejects_missing_configuration() -> None:
    with pytest.raises(ValueError):
        TelegramNotifier(TelegramConfig("", "123"))
    with pytest.raises(ValueError):
        TelegramNotifier(TelegramConfig(SECRET_TOKEN, ""))


def test_notifier_sends_rendered_message_to_configured_chat(offer: Offer, store: Store) -> None:
    store.observe(offer, True, Decimal("100"))
    transport = RecordingTransport()
    notifier = TelegramNotifier(TelegramConfig(SECRET_TOKEN, "123"), transport=transport)

    deliver_pending(store, notifier)

    assert len(transport.calls) == 1
    chat_id, text, _timeout = transport.calls[0]
    assert chat_id == "123"
    assert "Sunny Demo" in text
    assert store.pending() == []


def test_notifier_failure_leaves_notification_pending_and_does_not_raise(
    offer: Offer, store: Store
) -> None:
    store.observe(offer, True, Decimal("100"))
    notifier = TelegramNotifier(TelegramConfig(SECRET_TOKEN, "123"), transport=FailingTransport())

    deliver_pending(store, notifier)

    assert len(store.pending()) == 1


def test_notifier_failure_never_logs_the_bot_token(
    offer: Offer, store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    store.observe(offer, True, Decimal("100"))
    notifier = TelegramNotifier(TelegramConfig(SECRET_TOKEN, "123"), transport=FailingTransport())

    with caplog.at_level(logging.ERROR):
        deliver_pending(store, notifier)

    assert SECRET_TOKEN not in caplog.text


def test_urllib_transport_sends_expected_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_urlopen(request: object, timeout: float | None = None) -> FakeResponse:
        captured["url"] = request.full_url  # type: ignore[attr-defined]
        captured["body"] = json.loads(request.data)  # type: ignore[attr-defined]
        return FakeResponse(200, json.dumps({"ok": True}).encode())

    monkeypatch.setattr("travel_deal_agent.notifications.urlopen", fake_urlopen)
    transport = UrllibTelegramTransport(SECRET_TOKEN)

    transport.send_message("123", "hello world", 5.0)

    assert captured["body"] == {"chat_id": "123", "text": "hello world"}
    assert SECRET_TOKEN in captured["url"]


def test_urllib_transport_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: object, timeout: float | None = None) -> FakeResponse:
        raise URLError(TimeoutError("timed out"))

    monkeypatch.setattr("travel_deal_agent.notifications.urlopen", fake_urlopen)
    transport = UrllibTelegramTransport(SECRET_TOKEN)

    with pytest.raises(TelegramDeliveryError) as exc_info:
        transport.send_message("123", "hello", 5.0)

    assert SECRET_TOKEN not in str(exc_info.value)


def test_urllib_transport_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: object, timeout: float | None = None) -> FakeResponse:
        body = io.BytesIO(json.dumps({"ok": False, "description": "chat not found"}).encode())
        raise HTTPError("https://api.telegram.org/hidden", 400, "Bad Request", {}, body)  # type: ignore[arg-type]

    monkeypatch.setattr("travel_deal_agent.notifications.urlopen", fake_urlopen)
    transport = UrllibTelegramTransport(SECRET_TOKEN)

    with pytest.raises(TelegramDeliveryError) as exc_info:
        transport.send_message("123", "hello", 5.0)

    assert SECRET_TOKEN not in str(exc_info.value)
    assert "chat not found" in str(exc_info.value)


def test_urllib_transport_raises_on_api_level_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: object, timeout: float | None = None) -> FakeResponse:
        body = json.dumps({"ok": False, "description": "Forbidden: bot was blocked by the user"})
        return FakeResponse(200, body.encode())

    monkeypatch.setattr("travel_deal_agent.notifications.urlopen", fake_urlopen)
    transport = UrllibTelegramTransport(SECRET_TOKEN)

    with pytest.raises(TelegramDeliveryError) as exc_info:
        transport.send_message("123", "hello", 5.0)

    assert SECRET_TOKEN not in str(exc_info.value)
    assert "blocked" in str(exc_info.value)
