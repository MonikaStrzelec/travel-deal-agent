"""Direct coverage for provider registry factory dispatch.

The registry used to decide whether to apply its built-in itaka/rainbow/tui/
wakacje.pl special-casing by comparing the `factories` argument to
`DEFAULT_FACTORIES` with `is` (identity, not equality). That meant a caller
passing a fresh mapping with the exact same contents as `DEFAULT_FACTORIES`
(a different object, same data) silently lost the built-in dispatch. The fix
makes the switch depend only on whether `factories` was omitted (`None`),
never on the identity or contents of whatever mapping is passed. These tests
pin that contract directly, rather than relying on downstream behavior.
"""

from collections.abc import Callable

import pytest

from travel_deal_agent.config_types import ProviderConfig
from travel_deal_agent.providers.base import Provider
from travel_deal_agent.providers.itaka import ItakaProvider
from travel_deal_agent.providers.mock import MockProvider
from travel_deal_agent.providers.registry import DEFAULT_FACTORIES, build_providers

ENABLED: ProviderConfig = {"enabled": True, "interval_seconds": 60}
DISABLED: ProviderConfig = {"enabled": False, "interval_seconds": 60}


def test_omitting_factories_builds_the_mock_provider() -> None:
    sources = build_providers({"mock": ENABLED})

    assert [source.name for source in sources] == ["mock"]
    assert isinstance(sources[0], MockProvider)


def test_omitting_factories_applies_the_builtin_itaka_dispatch() -> None:
    sources = build_providers({"itaka": ENABLED})

    assert [source.name for source in sources] == ["itaka"]
    assert isinstance(sources[0], ItakaProvider)


def test_disabled_unknown_provider_is_skipped_without_checking_factories() -> None:
    # A disabled entry is skipped before any factory lookup, built-in or not.
    sources = build_providers({"not-a-real-provider": DISABLED})

    assert sources == []


def test_custom_factories_fully_replace_the_builtin_dispatch() -> None:
    class AdditionalProvider(MockProvider):
        name = "additional"

    sources = build_providers({"additional": ENABLED}, {"additional": AdditionalProvider})

    assert [source.name for source in sources] == ["additional"]


def test_custom_factories_reject_names_the_builtin_dispatch_would_have_handled() -> None:
    class AdditionalProvider(MockProvider):
        name = "additional"

    with pytest.raises(ValueError, match="No implementation"):
        build_providers({"itaka": ENABLED}, {"additional": AdditionalProvider})


def test_a_mapping_with_the_same_contents_as_default_factories_is_not_treated_as_default() -> None:
    # A fresh dict equal in content to DEFAULT_FACTORIES, but not the same
    # object. Before the fix, `factories is DEFAULT_FACTORIES` was False here,
    # so this silently behaved the same as any other explicit override --
    # which this test now asserts directly, instead of leaving it implicit.
    equivalent_factories: dict[str, Callable[[], Provider]] = {"mock": MockProvider}
    assert equivalent_factories == DEFAULT_FACTORIES
    assert equivalent_factories is not DEFAULT_FACTORIES

    with pytest.raises(ValueError, match="No implementation"):
        build_providers({"itaka": ENABLED}, equivalent_factories)


def test_passing_default_factories_explicitly_does_not_enable_builtin_dispatch() -> None:
    # Even the exact DEFAULT_FACTORIES object, passed explicitly rather than
    # omitted, no longer triggers the built-in itaka/rainbow/tui/wakacje.pl
    # dispatch: the switch is "was factories omitted", not object identity.
    with pytest.raises(ValueError, match="No implementation"):
        build_providers({"itaka": ENABLED}, DEFAULT_FACTORIES)


def test_factory_name_mismatch_is_rejected() -> None:
    class WrongName(MockProvider):
        name = "mock"

    with pytest.raises(ValueError, match="mismatch"):
        build_providers({"other": ENABLED}, {"other": WrongName})
