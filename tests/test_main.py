"""The MOCK DATA warning must reflect whether any real provider actually ran."""

import pytest

from travel_deal_agent.__main__ import _mock_data_warning_applies
from travel_deal_agent.config_types import ProviderConfig

ENABLED: ProviderConfig = {"enabled": True, "interval_seconds": 3600}
DISABLED: ProviderConfig = {"enabled": False, "interval_seconds": 3600}


@pytest.mark.parametrize(
    "providers,expected",
    [
        pytest.param({"mock": ENABLED, "wakacje.pl": DISABLED}, True, id="mock_only"),
        pytest.param({"mock": DISABLED, "wakacje.pl": ENABLED}, False, id="real_provider_alone"),
        pytest.param({"mock": ENABLED, "wakacje.pl": ENABLED}, False, id="mock_alongside_real"),
        # Mock itself is not enabled, so it never ran either.
        pytest.param({"mock": DISABLED, "wakacje.pl": DISABLED}, False, id="nothing_enabled"),
        # An unrelated, made-up real provider name -- proves this isn't
        # special-cased to any specific provider (e.g. only itaka/rainbow).
        pytest.param(
            {"mock": ENABLED, "some_future_source": ENABLED}, False, id="generic_with_mock"
        ),
        pytest.param(
            {"mock": DISABLED, "some_future_source": ENABLED}, False, id="generic_without_mock"
        ),
    ],
)
def test_mock_data_warning_applies_only_when_mock_is_the_only_source(
    providers: dict[str, ProviderConfig], expected: bool
) -> None:
    assert _mock_data_warning_applies(providers) is expected
