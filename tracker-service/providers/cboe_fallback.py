"""
CBOE delayed-quote fallback provider.

Same free delayed feed the Phase 1 discovery scanner uses
(https://cdn.cboe.com/api/global/delayed_quotes/options/{SYMBOL}.json).
Used ONLY as a labelled fallback when the real-time provider is DISCONNECTED, so an
open paper position keeps getting *some* honest (delayed) observation rather than a
gap — every such quote is stamped MODE_DELAYED_FALLBACK so it can never be mistaken
for real-time, and the worker records the transition as a FALLBACK_TO_CBOE event.

No credentials. Stdlib only.
"""
from __future__ import annotations
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Iterable, List, Dict

from .base import Provider, Quote, ContractRef, MODE_DELAYED_FALLBACK, register

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@register("cboe")
class CboeFallbackProvider(Provider):
    name = "cboe"
    default_mode = MODE_DELAYED_FALLBACK
    secret_env = None   # public delayed CDN — no credential

    def __init__(self, timeout: float = 10.0, **kwargs):
        super().__init__(**kwargs)
        self._timeout = timeout

    def connect(self) -> None:
        self._connected = True  # no auth; the public CDN is the "connection"

    def _fetch_symbol(self, symbol: str) -> dict:
        url = CBOE_URL.format(symbol=symbol)
        req = urllib.request.Request(url, headers={"User-Agent": "scanner-terminal-tracker"})
        with urllib.request.urlopen(req, timeout=self._timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def get_quotes(self, refs: Iterable[ContractRef]) -> List[Quote]:
        refs = list(refs)
        now = _now_utc_iso()
        # group by underlying symbol so we fetch each chain once
        by_symbol: Dict[str, List[ContractRef]] = {}
        for r in refs:
            by_symbol.setdefault(r.symbol, []).append(r)

        out: List[Quote] = []
        for symbol, group in by_symbol.items():
            try:
                payload = self._fetch_symbol(symbol)
                data = (payload or {}).get("data") or {}
                pqt = data.get("last_trade_time")   # CBOE snapshot ref time (US/Eastern, naive)
                options = {o.get("option"): o for o in (data.get("options") or [])}
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                # even the fallback failed for this symbol — emit ok=False, no fabrication
                for r in group:
                    out.append(Quote(contract_id=r.contract_id, provider=self.name,
                                     mode=MODE_DELAYED_FALLBACK, ok=False, ingestion_ts=now,
                                     dte=r.dte, note="CBOE fallback fetch failed"))
                continue

            for r in group:
                o = options.get(r.contract_id)
                if not o:
                    out.append(Quote(contract_id=r.contract_id, provider=self.name,
                                     mode=MODE_DELAYED_FALLBACK, ok=False, ingestion_ts=now,
                                     dte=r.dte, note="contract not found in CBOE chain"))
                    continue
                bid = o.get("bid"); ask = o.get("ask")
                mid = round((bid + ask) / 2, 4) if (bid is not None and ask is not None) else None
                has_px = bid is not None or ask is not None
                out.append(Quote(
                    contract_id=r.contract_id, provider=self.name, mode=MODE_DELAYED_FALLBACK,
                    ok=bool(has_px), provider_quote_ts=pqt, ingestion_ts=now,
                    bid=bid, ask=ask, mid=mid,
                    underlying=data.get("current_price") or data.get("close"),
                    iv=o.get("iv"), delta=o.get("delta"), theta=o.get("theta"),
                    dte=r.dte,
                    note=None if has_px else "no bid/ask in CBOE chain",
                ))
        return out
