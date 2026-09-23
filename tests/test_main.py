"""The MOCK DATA warning must reflect whether any real provider actually ran."""

from travel_deal_agent.__main__ import _mock_data_warning_applies
from travel_deal_agent.config_types import ProviderConfig

ENABLED: ProviderConfig = {"enabled": True, "interval_seconds": 3600}
DISABLED: ProviderConfig = {"enabled": False, "interval_seconds": 3600}


def test_mock_only_shows_the_warning() -> None:
    # Arrange / Act / Assert
    assert _mock_data_warning_applies({"mock": ENABLED, "wakacje.pl": DISABLED}) is True


def test_a_real_provider_alone_hides_the_warning() -> None:
    # Arrange / Act / Assert
    assert _mock_data_warning_applies({"mock": DISABLED, "wakacje.pl": ENABLED}) is False


def test_mock_alongside_a_real_provider_hides_the_warning() -> None:
    # Arrange / Act / Assert
    assert _mock_data_warning_applies({"mock": ENABLED, "wakacje.pl": ENABLED}) is False


def test_nothing_enabled_hides_the_warning() -> None:
    # Arrange / Act / Assert: mock itself is not enabled, so it never ran either.
    assert _mock_data_warning_applies({"mock": DISABLED, "wakacje.pl": DISABLED}) is False


def test_generic_over_provider_names_not_hardcoded_to_itaka_or_rainbow() -> None:
    # Arrange: an unrelated, made-up real provider name -- proves this isn't
    # special-cased to any specific provider (e.g. only itaka/rainbow).
    assert _mock_data_warning_applies({"mock": ENABLED, "some_future_source": ENABLED}) is False
    assert _mock_data_warning_applies({"mock": DISABLED, "some_future_source": ENABLED}) is False
