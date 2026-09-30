"""Offline fixtures shared by unit and integration tests."""

import json
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from travel_deal_agent.config import Settings, load_settings
from travel_deal_agent.models import Offer
from travel_deal_agent.providers.mock import MockProvider
from travel_deal_agent.storage import Store


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket.socket, "sendto", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    # Browser subprocesses have their own sockets; prevent accidental live browser startup too.
    monkeypatch.setattr("travel_deal_agent.providers.rainbow_browser.sync_playwright", blocked)
    monkeypatch.setattr("travel_deal_agent.providers.tui_browser.sync_playwright", blocked)


@pytest.fixture(autouse=True)
def no_google_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real local .env must never make a test call the live Google Places API.

    `config.load_settings()` calls `load_dotenv(..., override=False)`, which
    would otherwise leak a developer's real key from `.env` into the process
    environment for the rest of the test session. Individual Google tests
    that need a key set it explicitly via `monkeypatch.setenv`.

    Deliberately `setenv("", ...)`, not `delenv`: `load_dotenv(override=False)`
    only skips a key that is already *present* in `os.environ` -- an unset key
    is still filled in from `.env` on the next `load_settings()` call, which
    would silently undo a `delenv` the moment any fixture in this test loads
    settings.
    """
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "")


TEST_CONFIG = Path(__file__).resolve().parent / "fixtures" / "test_config.json"

RawConfig = dict[str, Any]
WriteConfig = Callable[[Callable[[RawConfig], None]], Path]


def _use_config(path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDA_CONFIG", str(path))
    monkeypatch.setenv("TDA_DATABASE", str(tmp_path / "offers.sqlite3"))
    monkeypatch.setenv("TDA_LOG_LEVEL", "INFO")
    # Points at a file that does not exist by default (the normal "no active
    # watchlist" state) -- never the real project's hotel_watchlist.json.
    # See `write_hotel_watchlist` for tests that need one to exist.
    monkeypatch.setenv("TDA_HOTEL_WATCHLIST", str(tmp_path / "hotel_watchlist.json"))


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Load a frozen test configuration, independent of the production config.json.

    Business-rule changes to the real config.json (enabled providers, prices,
    page budgets, ...) must never affect this fixture; see
    tests/fixtures/test_config.json.
    """
    _use_config(TEST_CONFIG, tmp_path, monkeypatch)
    return load_settings()


@pytest.fixture
def write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WriteConfig:
    """Return a writer for a mutated copy of the frozen test configuration.

    The callable applies `mutate` to a fresh copy of tests/fixtures/test_config.json,
    writes it to `tmp_path / "config.json"` and points TDA_CONFIG (plus a
    temporary TDA_DATABASE) at it, so the next load_settings() sees exactly
    that configuration and never the production config.json.
    """

    def write(mutate: Callable[[RawConfig], None]) -> Path:
        raw: RawConfig = json.loads(TEST_CONFIG.read_text(encoding="utf-8-sig"))
        mutate(raw)
        path = tmp_path / "config.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        _use_config(path, tmp_path, monkeypatch)
        return path

    return write


WriteHotelWatchlist = Callable[[object], Path]


@pytest.fixture
def write_hotel_watchlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WriteHotelWatchlist:
    """Write `content` (typically a list of watchlist entries, or raw text via
    a plain string) as this test's hotel_watchlist.json.

    Independent of `write_config`/`settings`: those already point
    TDA_HOTEL_WATCHLIST at `tmp_path / "hotel_watchlist.json"` (a path that
    does not exist by default); this only creates that file.
    """

    def write(content: object) -> Path:
        path = tmp_path / "hotel_watchlist.json"
        path.write_text(content if isinstance(content, str) else json.dumps(content), "utf-8")
        monkeypatch.setenv("TDA_HOTEL_WATCHLIST", str(path))
        return path

    return write


@pytest.fixture
def offer() -> Offer:
    return MockProvider().fetch()[0]


@pytest.fixture
def store(settings: Settings) -> Iterator[Store]:
    with Store(settings.database) as database:
        yield database
