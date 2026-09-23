"""Offline fixtures shared by unit and integration tests."""

import socket
from collections.abc import Iterator
from pathlib import Path

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


TEST_CONFIG = Path(__file__).resolve().parent / "fixtures" / "test_config.json"


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Load a frozen test configuration, independent of the production config.json.

    Business-rule changes to the real config.json (enabled providers, prices,
    page budgets, ...) must never affect this fixture; see
    tests/fixtures/test_config.json.
    """
    monkeypatch.setenv("TDA_CONFIG", str(TEST_CONFIG))
    monkeypatch.setenv("TDA_DATABASE", str(tmp_path / "offers.sqlite3"))
    monkeypatch.setenv("TDA_LOG_LEVEL", "INFO")
    return load_settings()


@pytest.fixture
def offer() -> Offer:
    return MockProvider().fetch()[0]


@pytest.fixture
def store(settings: Settings) -> Iterator[Store]:
    with Store(settings.database) as database:
        yield database
