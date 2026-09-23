"""CLI --watch safety gating, exercised without any live scraping or endless loop."""

import json
import sys
from pathlib import Path

import pytest

from travel_deal_agent import __main__ as cli
from travel_deal_agent.config import ROOT
from travel_deal_agent.scheduler import Scheduler


def _write_config(tmp_path: Path, **provider_overrides: dict[str, object]) -> Path:
    raw = json.loads((ROOT / "config.json").read_text())
    for name, overrides in provider_overrides.items():
        raw["providers"][name].update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    return path


def _prepare_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_path: Path, argv: list[str]
) -> None:
    monkeypatch.setenv("TDA_CONFIG", str(config_path))
    monkeypatch.setenv("TDA_DATABASE", str(tmp_path / "offers.sqlite3"))
    monkeypatch.setenv("TDA_LOG_LEVEL", "INFO")
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["travel-deal-agent", *argv])
    # run_forever would loop forever; stub it so main() returns once the gate is passed.
    monkeypatch.setattr(Scheduler, "run_forever", lambda self: None)


def test_watch_with_itaka_enabled_is_no_longer_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange
    config_path = _write_config(tmp_path, itaka={"enabled": True})
    _prepare_cli(monkeypatch, tmp_path, config_path, ["--watch"])
    # Act / Assert: no SystemExit raised, run_forever (stubbed) is reached and returns.
    cli.main()


def test_watch_with_rainbow_enabled_is_still_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange
    config_path = _write_config(tmp_path, rainbow={"enabled": True})
    _prepare_cli(monkeypatch, tmp_path, config_path, ["--watch"])
    # Act / Assert
    with pytest.raises(SystemExit):
        cli.main()
