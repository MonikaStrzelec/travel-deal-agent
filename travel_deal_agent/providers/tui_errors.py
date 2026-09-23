"""Technical outcomes are distinct from a successful empty search."""


class TuiError(RuntimeError):
    """Source integration failure; never equivalent to zero results."""


class TuiTimeout(TuiError):
    """A bounded browser operation did not complete in time."""


class TuiStructureError(TuiError):
    """Observed page/response structure no longer satisfies the contract."""


class TuiBlocked(TuiError):
    """Access denied or human verification; no bypass or automatic retry."""
