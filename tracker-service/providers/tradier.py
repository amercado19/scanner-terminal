"""
Tradier real-time option-quote adapter (REFERENCE real-time provider for Phase 2).

Chosen as the reference adapter because Tradier offers a plain REST endpoint for
option quotes that a persistent worker can POLL on a short interval without holding
a socket open, and a brokerage/market-data account can enable real-time (not just
delayed) quotes. Polygon is a valid alternative (WebSocket or REST) — implement a
`polygon.py` against the same Provider interface and the worker is unchanged.

Endpoint (polling): GET https://api.tradier.com/v1/markets/quotes
  headers: Authorization: Bearer <TRADIER_TOKEN>, Accept: application/json
  params:  symbols=<comma-separated OCC symbols>, greeks=true
Real-time vs delayed depends on the ACCOUNT's market-data entitlement; this adapter
reads the entitlement Tradier reports and labels the mode accordingly — it does not
assume real-time.

SECURITY: the token is read ONLY from the TRADIER_TOKEN environment variable / platform
secret. It is never hardcoded, never logged, and never written to the output repo.
"""
from __future__ import annotations
import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Iterable, List

from .base import Provider, Quote, ContractRef, MODE_REALTIME, MODE_DELAYED, register

QUOTES_PATH = "/v1/markets/quotes"
ENV_TOKEN = "TRADIER_TOKEN"
ENV_ENV = "TRADIER_ENV"            # "production" (real-time, funded acct) | "sandbox" (delayed)
PROD_BASE = "https://api.tradier.com"
SANDBOX_BASE = "https://sandbox.tradier.com"


def resolve_base(env_value: str | None = None) -> str:
    """Pick the Tradier API base from TRADIER_ENV / TRADIER_BASE. Production = real-time (with a
    funded brokerage entitlement); sandbox = delayed. An explicit TRADIER_BASE always wins."""
    explicit = os.environ.get("TRADIER_BASE")
    if explicit:
        return explicit.rstrip("/")
    env = (env_value if env_value is not None else os.environ.get(ENV_ENV) or "production").lower()
    return SANDBOX_BASE if env in ("sandbox", "sbx", "dev") else PROD_BASE


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_quotes(payload: dict, refs, provider_name: str, default_mode: str, now: str):
    """PURE, network-free normalization of a Tradier /markets/quotes response into Quote objects.
    Extracted so it can be unit-tested with a sample payload. NEVER fabricates: a contract absent
    from the payload (or without bid/ask) becomes ok=False with no price."""
    refs = list(refs)
    by_id = {r.contract_id: r for r in refs}
    quotes_node = ((payload or {}).get("quotes") or {}).get("quote")
    if quotes_node is None:
        rows = []
    elif isinstance(quotes_node, list):
        rows = quotes_node
    else:
        rows = [quotes_node]

    seen = {}
    for row in rows:
        sym = row.get("symbol")
        if not sym:
            continue
        bid = row.get("bid")
        ask = row.get("ask")
        mid = round((float(bid) + float(ask)) / 2, 4) if (bid is not None and ask is not None) else None
        # Tradier flags delayed quotes for the account/entitlement; default to real-time only when
        # the feed does NOT say delayed.
        if "delayed" in row:
            delayed_flag = bool(row.get("delayed"))
        else:
            delayed_flag = default_mode == MODE_DELAYED
        mode = MODE_DELAYED if delayed_flag else MODE_REALTIME
        greeks = row.get("greeks") or {}
        pqt = None
        tms = row.get("trade_date") or row.get("bid_date") or row.get("ask_date")
        if tms:
            try:
                pqt = datetime.fromtimestamp(int(tms) / 1000, tz=timezone.utc).isoformat()
            except Exception:
                pqt = None
        has_px = bid is not None or ask is not None
        seen[sym] = Quote(
            contract_id=sym, provider=provider_name, mode=mode, ok=bool(has_px),
            provider_quote_ts=pqt, ingestion_ts=now,
            bid=float(bid) if bid is not None else None,
            ask=float(ask) if ask is not None else None,
            mid=mid, underlying=None,
            iv=greeks.get("mid_iv"), delta=greeks.get("delta"), theta=greeks.get("theta"),
            dte=by_id[sym].dte if sym in by_id else None,
            note=None if has_px else "no bid/ask returned for this contract",
        )

    out = []
    for r in refs:
        if r.contract_id in seen:
            out.append(seen[r.contract_id])
        else:
            out.append(Quote(contract_id=r.contract_id, provider=provider_name,
                             mode=default_mode, ok=False, ingestion_ts=now, dte=r.dte,
                             note="contract not present in provider response"))
    return out


@register("tradier")
class TradierProvider(Provider):
    name = "tradier"
    default_mode = MODE_REALTIME
    # secret this adapter reads (env / platform secret store only)
    secret_env = ENV_TOKEN

    def __init__(self, token: str | None = None, timeout: float = 8.0, env: str | None = None, **kwargs):
        super().__init__(**kwargs)
        # credentials come from the environment / platform secret store ONLY
        self._token = token or os.environ.get(ENV_TOKEN)
        self._timeout = timeout
        self._env = env if env is not None else os.environ.get(ENV_ENV, "production")
        self._base = resolve_base(self._env)
        # sandbox is a DELAYED feed; production with a funded entitlement is real-time
        self.default_mode = MODE_DELAYED if resolve_base(self._env) == SANDBOX_BASE else MODE_REALTIME

    def configured(self) -> bool:
        """Readiness for LIVE operation: the secret must be present. The worker checks this at
        startup and stays NOT_CONFIGURED (dormant) rather than requesting quotes without a token."""
        return bool(self._token)

    def connect(self) -> None:
        if not self._token:
            raise RuntimeError(
                f"{ENV_TOKEN} not set — the real-time tracker cannot authenticate. "
                "Set it as an environment variable / platform secret (see README). "
                "No fallback credentials are used."
            )
        # a cheap validating call could go here; kept side-effect-free for the scaffold
        self._connected = True

    def _http_get(self, symbols: List[str]) -> dict:
        params = "symbols=" + ",".join(symbols) + "&greeks=true"
        url = f"{self._base}{QUOTES_PATH}?{params}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=self._timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def get_quotes(self, refs: Iterable[ContractRef]) -> List[Quote]:
        refs = list(refs)
        if not refs:
            return []
        if not self._connected:
            # total feed loss for THIS call — signal it; the worker decides fallback.
            raise RuntimeError("TradierProvider.get_quotes called while disconnected")

        symbols = [r.contract_id for r in refs]
        now = _now_utc_iso()
        try:
            payload = self._http_get(symbols)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # a transport failure is a TOTAL loss for this poll — raise so the worker
            # marks DISCONNECTED / STALE rather than fabricating quotes.
            raise RuntimeError(f"Tradier transport error: {e}") from e
        # pure, tested normalization — no fabrication
        return normalize_quotes(payload, refs, self.name, self.default_mode, now)
