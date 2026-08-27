"""Provider package for the real-time active-position tracker.

Importing this package imports every adapter module, which self-registers its provider via
@register. The worker resolves a provider ONLY by config name through the registry below — it
never imports a concrete adapter, so a new broker is a one-file addition with zero worker/engine
changes.
"""
from .base import (
    Provider, Quote, ContractRef,
    MODE_REALTIME, MODE_DELAYED, MODE_DELAYED_FALLBACK,
    LIVE, DELAYED, STALE, DISCONNECTED,
    FALLBACK_TO_CBOE, EXIT_FIRST_OBSERVED, SIMULATED_CLOSED,
    PROVIDERS, register, available_providers, create_provider,
)

# import adapters for their side-effect: registration. Order does not matter.
from . import cboe_fallback   # noqa: F401  -> "cboe"   (real, delayed fallback floor)
from . import tradier         # noqa: F401  -> "tradier" (real, reference real-time)
from . import stubs           # noqa: F401  -> "polygon","alpaca","ibkr","schwab" (pluggable stubs)

__all__ = [
    "Provider", "Quote", "ContractRef",
    "MODE_REALTIME", "MODE_DELAYED", "MODE_DELAYED_FALLBACK",
    "LIVE", "DELAYED", "STALE", "DISCONNECTED",
    "FALLBACK_TO_CBOE", "EXIT_FIRST_OBSERVED", "SIMULATED_CLOSED",
    "PROVIDERS", "register", "available_providers", "create_provider",
    "get_provider",
]


def get_provider(name: str, **kwargs) -> Provider:
    """Back-compat alias for create_provider(): resolve a registered provider by config name."""
    return create_provider(name, **kwargs)
