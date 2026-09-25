"""Behavioral regressions for configuration and dependency boundaries."""

import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from conftest import WriteConfig
from travel_deal_agent.config import ROOT, Settings, load_settings
from travel_deal_agent.config_types import ProviderConfig
from travel_deal_agent.models import Offer
from travel_deal_agent.notifications import LogNotifier, deliver_pending
from travel_deal_agent.providers.mock import MockProvider
from travel_deal_agent.providers.registry import build_providers
from travel_deal_agent.scheduler import Scheduler
from travel_deal_agent.storage import Store


def test_registry_accepts_an_injected_source_factory() -> None:
    class AdditionalProvider(MockProvider):
        name = "additional"

    config: dict[str, ProviderConfig] = {
        "additional": {"enabled": True, "interval_seconds": 60},
        "unused": {"enabled": False, "interval_seconds": 60},
    }

    sources = build_providers(config, {"additional": AdditionalProvider})

    assert [source.name for source in sources] == ["additional"]


def test_registry_rejects_unknown_enabled_source() -> None:
    config: dict[str, ProviderConfig] = {"missing": {"enabled": True, "interval_seconds": 60}}

    with pytest.raises(ValueError, match="No implementation"):
        build_providers(config)


def test_registered_provider_without_a_rating_rule_defaults_to_disabled(
    settings: Settings, write_config: WriteConfig
) -> None:
    # A provider can be registered under `providers` with no agreed native-
    # rating threshold yet. Without a default, `rating_matches()` would treat
    # "no rule at all" as an unconditional reject (`rules.get(name) is None ->
    # False`), silently blocking every offer from that provider on top of any
    # intentional price_is_complete gate. The default must never invent a
    # threshold -- it mirrors the explicit `{"enabled": false, ...}` entry
    # "mock" already has in config.json. TUI itself now carries this same
    # `{"enabled": false, ...}` shape as an explicit config.json entry (its
    # scale is confirmed by captured data -- see provider_ratings.tui), so
    # this test exercises the fallback with a synthetic unconfigured provider
    # instead of relying on TUI staying unconfigured.
    write_config(
        lambda raw: raw["providers"].update(
            {"future-source": {"enabled": False, "interval_seconds": 3600}}
        )
    )

    future_settings = load_settings()

    assert "future-source" in future_settings.providers
    assert future_settings.filters["provider_ratings"]["future-source"] == {
        "enabled": False,
        "scale": None,
        "price_bands": [],
    }
    # A provider name nobody registered is unaffected and stays absent.
    assert "not-a-real-provider" not in future_settings.filters["provider_ratings"]
    # TUI's own rule is now explicit in config.json, not injected.
    assert settings.filters["provider_ratings"]["tui"] == {
        "enabled": False,
        "scale": {"min": 1.0, "max": 5.0},
        "price_bands": [],
    }


def test_observation_rolls_back_when_alert_policy_fails(
    offer: Offer, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_policy(*args: object) -> None:
        raise ValueError("alert policy failure")

    monkeypatch.setattr("travel_deal_agent.storage.classify_alert", failing_policy)
    with pytest.raises(ValueError, match="alert policy"):
        store.observe(offer, True)

    assert store.get_offer(offer.provider, offer.offer_id) is None
    assert store.price_history(offer.provider, offer.offer_id) == []
    assert store.pending() == []


def test_storage_failure_is_not_swallowed_by_scheduler(settings: Settings, store: Store) -> None:
    # Scoped to the one provider this test wires up, with enabled explicitly
    # forced True -- independent of whatever the real config.json currently
    # has (it disables mock so Wakacje.pl can run alone). Without this, mock
    # would be skipped entirely and the ProgrammingError below would come
    # from deliver_pending's own closed-store touch instead of from the
    # scheduler actually trying to check this provider's run state.
    settings = replace(
        settings, providers={"mock": {**settings.providers["mock"], "enabled": True}}
    )
    scheduler = Scheduler(settings, [MockProvider()], store, LogNotifier())
    store.close()

    with pytest.raises(sqlite3.ProgrammingError):
        scheduler.run_once()


def test_storage_failure_is_not_swallowed_by_delivery(store: Store) -> None:
    store.close()

    with pytest.raises(sqlite3.ProgrammingError):
        deliver_pending(store, LogNotifier())


@pytest.mark.parametrize("invalid_value", ["2", True, None, []])
def test_config_does_not_coerce_invalid_traveler_types(
    write_config: WriteConfig, invalid_value: object
) -> None:
    write_config(lambda raw: raw["filters"].update(people=invalid_value))

    with pytest.raises(ValueError, match="filters.people"):
        load_settings()


def test_config_rejects_misspelled_keys(write_config: WriteConfig) -> None:
    write_config(lambda raw: raw["filters"].update(min_dayz=7))

    with pytest.raises(ValueError, match="min_dayz"):
        load_settings()


def test_tui_detail_requests_are_capped_at_three(write_config: WriteConfig) -> None:
    # A simple, explicit browser-navigation budget: TUI's max_detail_requests is
    # each a full Playwright navigation (heavier than an HTTP request), so this
    # caps the whole cycle at 1 fixed listing navigation + at most 3 detail ones,
    # regardless of how the value is configured.
    write_config(lambda raw: raw["providers"]["tui"].update(max_detail_requests=4))

    with pytest.raises(ValueError, match="max_detail_requests must be at most 3"):
        load_settings()


def test_tui_cycle_seconds_must_be_positive(write_config: WriteConfig) -> None:
    # The aggregate cycle deadline (see experiments/tui/CURRENT_STATE.md) reuses
    # the existing generic "HTTP limits must be positive" check in
    # config.py::validate_options -- no TUI-specific check was added for this.
    write_config(lambda raw: raw["providers"]["tui"].update(cycle_seconds=0))

    with pytest.raises(ValueError, match="positive"):
        load_settings()


def test_tui_cycle_seconds_rejects_wrong_type(write_config: WriteConfig) -> None:
    write_config(lambda raw: raw["providers"]["tui"].update(cycle_seconds="90"))

    with pytest.raises(ValueError, match="providers.tui.cycle_seconds"):
        load_settings()


def test_itaka_max_requests_must_cover_pages_and_detail_requests(write_config: WriteConfig) -> None:
    # One robots.txt fetch + max_pages listing pages + max_detail_requests detail
    # confirmations must all fit in max_requests (itaka.py fetch() shares one
    # RequestBudget across all of them); 1 + 2 + 1 = 4, so 3 is not enough.
    write_config(
        lambda raw: raw["providers"]["itaka"].update(
            max_pages=2, max_detail_requests=1, max_requests=3
        )
    )

    with pytest.raises(ValueError, match="ITAKA max_requests must cover"):
        load_settings()


def test_itaka_max_requests_exactly_covers_the_budget(write_config: WriteConfig) -> None:
    # max_pages=2 plus max_detail_requests=1 needs exactly
    # 4 requests (1 robots.txt + 2 listing + 1 detail); this must not raise.
    write_config(
        lambda raw: raw["providers"]["itaka"].update(
            max_pages=2, max_detail_requests=1, max_requests=4
        )
    )

    load_settings()


def test_itaka_unlimited_pages_skips_the_budget_arithmetic_check(write_config: WriteConfig) -> None:
    # max_pages=null means unbounded pagination -- the exact request count is
    # not known upfront (RequestBudget's own deadline/max_requests guard the
    # runtime instead), so the static arithmetic check does not apply.
    write_config(lambda raw: raw["providers"]["itaka"].update(max_pages=None, max_requests=1))

    load_settings()


def test_tui_provider_rejects_unknown_field(write_config: WriteConfig) -> None:
    write_config(lambda raw: raw["providers"]["tui"].update(cycle_secondss=90))

    with pytest.raises(ValueError, match="cycle_secondss"):
        load_settings()


def test_scheduler_uses_injected_calendar_and_sleep(settings: Settings, store: Store) -> None:
    today = date(2030, 1, 1)
    waits: list[float] = []

    def stop_after_first_wait(seconds: float) -> None:
        waits.append(seconds)
        raise KeyboardInterrupt

    # Scoped to the one provider this test wires up, with enabled explicitly
    # forced True -- independent of whatever the real config.json currently
    # has (it disables mock so Wakacje.pl can run alone).
    scoped_settings = replace(
        settings, providers={"mock": {**settings.providers["mock"], "enabled": True}}
    )
    scheduler = Scheduler(
        scoped_settings,
        [MockProvider(today=lambda: today)],
        store,
        LogNotifier(),
        clock=lambda: 1000,
        today=lambda: today,
        sleep=stop_after_first_wait,
    )

    with pytest.raises(KeyboardInterrupt):
        scheduler.run_forever()

    assert waits == [settings.scheduler["idle_poll_seconds"]]
    assert store.run_state("mock") == {"next_run": 1600, "failures": 0}


def test_duplicate_provider_names_are_rejected(settings: Settings, store: Store) -> None:
    with pytest.raises(ValueError, match="unique"):
        Scheduler(settings, [MockProvider(), MockProvider()], store, LogNotifier())


def test_settings_fixture_never_reads_the_production_config(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: guard the one file this must never touch while the shared
    # `settings` fixture is active, so an edit to production config.json's
    # `enabled`, `max_price`, `max_pages` etc. cannot cause an unrelated test
    # failure elsewhere (see tests/fixtures/test_config.json and conftest.py).
    production_config = ROOT / "config.json"
    original_read_text = Path.read_text

    def guarded_read_text(
        path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if path == production_config:
            raise AssertionError("must not read the production config.json during tests")
        return original_read_text(path, encoding, errors)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    # Act: reload settings while the env var TDA_CONFIG set up by the
    # `settings` fixture (pointing at the frozen test fixture) is still active.
    reloaded = load_settings()

    # Assert: succeeds without ever touching production config.json.
    assert reloaded == settings


def test_invalid_source_identity_does_not_enter_results(settings: Settings, store: Store) -> None:
    class MismatchedProvider(MockProvider):
        def fetch(self) -> list[Offer]:
            return [replace(offer, provider="wrong") for offer in super().fetch()]

    # Scoped to the one provider this test wires up, with enabled explicitly
    # forced True -- independent of whatever the real config.json currently
    # has (it disables mock so Wakacje.pl can run alone). Without this,
    # MismatchedProvider.fetch() would never even be called, and the
    # assertions below would hold vacuously instead of proving the identity
    # check in pipeline.filter_batch() actually rejects the mismatched offers.
    settings = replace(
        settings, providers={"mock": {**settings.providers["mock"], "enabled": True}}
    )
    scheduler = Scheduler(settings, [MismatchedProvider()], store, LogNotifier())

    result = scheduler.run_once()

    assert result == []
    assert store.pending() == []
