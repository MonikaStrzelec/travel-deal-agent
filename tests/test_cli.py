"""CLI --watch safety gating, exercised without any live scraping or endless loop."""

import sys
from pathlib import Path

import pytest

from conftest import WriteConfig
from travel_deal_agent import __main__ as cli
from travel_deal_agent.scheduler import Scheduler


def _prepare_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]) -> None:
    # TDA_CONFIG / TDA_DATABASE are already set by the `write_config` fixture.
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["travel-deal-agent", *argv])
    # run_forever would loop forever; stub it so main() returns once the gate is passed.
    monkeypatch.setattr(Scheduler, "run_forever", lambda self: None)


def test_watch_with_itaka_enabled_is_no_longer_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, write_config: WriteConfig
) -> None:
    # Arrange: the frozen test config has ITAKA disabled, so this enables it explicitly.
    write_config(lambda raw: raw["providers"]["itaka"].update(enabled=True))
    _prepare_cli(monkeypatch, tmp_path, ["--watch"])
    # Act / Assert: no SystemExit raised, run_forever (stubbed) is reached and returns.
    cli.main()


def test_watch_with_rainbow_enabled_is_still_blocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_config: WriteConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_config(lambda raw: raw["providers"]["rainbow"].update(enabled=True))
    _prepare_cli(monkeypatch, tmp_path, ["--watch"])
    with pytest.raises(SystemExit):
        cli.main()
    # Assert: rejected by the Rainbow gate, not by an unrelated configuration error.
    assert "Rainbow is manual-only" in capsys.readouterr().err
