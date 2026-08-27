"""
COPY-ME TEMPLATE for a new real-time provider adapter.

Adding a broker/data provider is a one-file change: copy this file to `providers/<name>.py`,
fill in `connect` and `get_quotes`, and it self-registers via @register. The worker, the config
layer, and the frozen stop engine are all UNCHANGED — only this adapter is new.

This template is intentionally NOT registered (name "template" is not wired) so it can't be
selected by accident. Delete this docstring line and add @register("<name>") when you build one.

Contract you must honour (enforced by the test-suite invariants):
  * Request ONLY the given contracts (they are the open paper positions) — never the whole market.
  * NEVER fabricate a quote. No fresh data for a contract => Quote(ok=False) with no bid/ask.
  * Stamp provider_quote_ts from the FEED; the worker computes observed lag.
  * Label mode honestly: MODE_REALTIME only when the feed truly is; delayed data => MODE_DELAYED.
  * Read any secret from the environment / platform secret store ONLY — never hard-code it.
  * Raise from get_quotes only on TOTAL feed loss (so the worker can mark DISCONNECTED / fall back);
    a single missing contract is ok=False, not an exception.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone
from typing import Iterable, List

from .base import Provider, Quote, ContractRef, MODE_REALTIME  # , register


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# @register("yourprovider")
class TemplateProvider(Provider):
    name = "template"
    default_mode = MODE_REALTIME
    secret_env = "YOURPROVIDER_TOKEN"

    def __init__(self, token: str | None = None, timeout: float = 8.0, **kwargs):
        super().__init__(**kwargs)
        self._token = token or os.environ.get(self.secret_env)
        self._timeout = timeout

    def connect(self) -> None:
        if not self._token:
            raise RuntimeError(f"{self.secret_env} not set — cannot authenticate. No fallback credential is used.")
        self._connected = True

    def get_quotes(self, refs: Iterable[ContractRef]) -> List[Quote]:
        refs = list(refs)
        now = _now_utc_iso()
        # 1) call your provider for exactly [r.contract_id for r in refs]
        # 2) build one Quote per ref; ok=False (no price) for any the feed didn't return
        out: List[Quote] = []
        for r in refs:
            out.append(Quote(
                contract_id=r.contract_id, provider=self.name, mode=self.default_mode,
                ok=False, ingestion_ts=now, dte=r.dte,
                note="template: implement get_quotes()",
            ))
        return out
