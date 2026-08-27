"""
Pluggable provider STUBS — Polygon, Alpaca, IBKR, Schwab.

These prove the abstraction: each is selectable by config name (`--provider polygon`, etc.)
and self-registers exactly like the real adapters, but its `get_quotes` is not yet implemented,
so it raises a clear NotImplementedError pointing at the one method to fill in. NOTHING in the
worker or the engine changes when one of these is finished — you implement `get_quotes` (and
usually `connect`) against the same `Provider` interface, and the tracker uses it unchanged.

To finish one: copy `template.py`, or fill in `get_quotes` here, reading the secret named in
`secret_env` from the environment / platform secret store ONLY (never hard-coded).

Robinhood is intentionally absent: it has no official, supported market-data API for unattended
use. If/when an official API exists, add `robinhood.py` the same way — the engine still won't change.
"""
from __future__ import annotations
import os
from typing import Iterable, List

from .base import Provider, Quote, ContractRef, register, MODE_REALTIME


class _UnimplementedProvider(Provider):
    """Shared behaviour for a registered-but-unbuilt adapter. Selectable by config; fails loudly
    and honestly rather than fabricating quotes."""
    default_mode = MODE_REALTIME
    secret_env: str | None = None
    docs_url = ""

    def connect(self) -> None:
        # surface the missing secret early, but the real blocker is the unimplemented feed
        self._connected = True

    def get_quotes(self, refs: Iterable[ContractRef]) -> List[Quote]:
        raise NotImplementedError(
            f"provider '{self.name}' is a registered stub — implement {type(self).__name__}.get_quotes() "
            f"against the Provider interface (see providers/template.py). "
            f"Secret it should read from the environment: {self.secret_env or '(none/TBD)'}. "
            f"Docs: {self.docs_url or 'n/a'}. The worker and engine do NOT change when you finish it."
        )


@register("polygon")   # a.k.a. Massive.com as of mid-2026
class PolygonProvider(_UnimplementedProvider):
    default_mode = MODE_REALTIME
    secret_env = "POLYGON_API_KEY"
    docs_url = "https://polygon.io/docs/options"


@register("alpaca")
class AlpacaProvider(_UnimplementedProvider):
    default_mode = MODE_REALTIME
    secret_env = "ALPACA_API_KEY"   # plus ALPACA_API_SECRET
    docs_url = "https://docs.alpaca.markets/docs/about-market-data-api"


@register("ibkr")
class IBKRProvider(_UnimplementedProvider):
    default_mode = MODE_REALTIME
    # IBKR needs a running Client Portal / TWS gateway session, not a simple token
    secret_env = "IBKR_GATEWAY_URL"
    docs_url = "https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/"


@register("schwab")
class SchwabProvider(_UnimplementedProvider):
    default_mode = MODE_REALTIME
    secret_env = "SCHWAB_APP_KEY"   # plus SCHWAB_APP_SECRET / OAuth refresh token
    docs_url = "https://developer.schwab.com/products/trader-api--individual"
