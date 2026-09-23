"""Technical outcomes are distinct from a successful empty search."""


class RainbowError(RuntimeError):
    """Source integration failure; never equivalent to zero results."""


class RainbowTimeout(RainbowError):
    """A bounded operation or scan deadline expired."""


class RainbowStructureError(RainbowError):
    """Observed controls or card structure no longer satisfy the contract."""


class RainbowBlocked(RainbowError):
    """Access denied or human verification; no bypass or automatic retry."""
