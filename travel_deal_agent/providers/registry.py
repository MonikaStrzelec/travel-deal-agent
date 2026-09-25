"""Application composition: add provider factories without changing the scheduler."""

from collections.abc import Callable, Mapping

from ..config_types import AttractivenessConfig, FilterConfig, ProviderConfig
from .base import Provider
from .itaka import ItakaProvider
from .mock import MockProvider

DEFAULT_FACTORIES: Mapping[str, Callable[[], Provider]] = {"mock": MockProvider}


def build_providers(
    configuration: Mapping[str, ProviderConfig],
    factories: Mapping[str, Callable[[], Provider]] | None = None,
    *,
    filters: FilterConfig | None = None,
    attractiveness: AttractivenessConfig | None = None,
) -> list[Provider]:
    """Instantiate enabled sources; reject unsupported names before doing any work.

    `factories` defaults to the built-in itaka/rainbow/tui/wakacje.pl dispatch
    below (plus `mock`, via DEFAULT_FACTORIES) when omitted. Passing any
    mapping explicitly replaces that dispatch entirely, regardless of whether
    its contents happen to match DEFAULT_FACTORIES -- the switch is whether
    `factories` was omitted, never the identity or contents of what's passed.
    """
    use_builtin_dispatch = factories is None
    resolved_factories = DEFAULT_FACTORIES if factories is None else factories
    providers: list[Provider] = []
    for name, config in configuration.items():
        if not config["enabled"]:
            continue
        if name == "itaka" and use_builtin_dispatch:
            # Unlike rainbow/tui/wakacje.pl, filters are optional here (see
            # ItakaProvider.__init__): they only improve which listing
            # candidate gets the scarce detail request, so a caller that omits
            # them (as this registry itself allows) still gets a working provider.
            providers.append(ItakaProvider(config, filters))
            continue
        if name == "rainbow" and use_builtin_dispatch:
            from .rainbow import RainbowProvider

            if filters is None:
                raise ValueError("Rainbow requires shared business filters")
            providers.append(RainbowProvider(config, filters))
            continue
        if name == "tui" and use_builtin_dispatch:
            from .tui import TuiProvider

            if filters is None:
                raise ValueError("TUI requires shared business filters")
            providers.append(TuiProvider(config, filters, attractiveness=attractiveness))
            continue
        if name == "wakacje.pl" and use_builtin_dispatch:
            from .wakacje import WakacjeProvider

            if filters is None:
                raise ValueError("Wakacje.pl requires shared business filters")
            providers.append(WakacjeProvider(config, filters))
            continue
        if name not in resolved_factories:
            raise ValueError(f"No implementation for enabled provider: {name}")
        provider = resolved_factories[name]()
        if provider.name != name:
            raise ValueError(f"Provider factory name mismatch: {name}")
        providers.append(provider)
    return providers
