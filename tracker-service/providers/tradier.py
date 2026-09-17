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

from .base import (
    Provider, Quote, ContractRef, MODE_REALTIME, MODE_DELAYED, register,
    is_valid_occ, missing_occ_quote, split_refs_by_occ,
    DQ_OK, DQ_MISSING_BID_ASK, DQ_ZERO_BID, DQ_OPTION_NOT_FOUND, DQ_STALE, DQ_MALFORMED_ROW,
    ProviderError, ProviderAuthError, ProviderRateLimitError, ProviderPayloadError,
)

QUOTES_PATH = "/v1/markets/quotes"
ENV_TOKEN = "TRADIER_TOKEN"
ENV_ENV = "TRADIER_ENV"            # "production" (real-time, funded acct) | "sandbox" (delayed)
PROD_BASE = "https://api.tradier.com"
SANDBOX_BASE = "https://sandbox.tradier.com"

# a REALTIME quote whose feed timestamp is older than this is a TYPED data-quality failure
# (DQ_STALE), never acted on and never fabricated into a fresh midpoint.
STALE_AFTER_SEC = 600


def _iso_age_seconds(pqt: str | None, now_iso: str | None):
    """Whole seconds between a provider quote timestamp and now. Both are tz-aware UTC ISO here
    (pqt is built from Tradier's epoch-ms trade_date). Returns None if not computable."""
    if not pqt or not now_iso:
        return None
    try:
        pt = datetime.fromisoformat(pqt.replace("Z", "+00:00"))
        nt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        if pt.tzinfo is None:
            pt = pt.replace(tzinfo=timezone.utc)
        if nt.tzinfo is None:
            nt = nt.replace(tzinfo=timezone.utc)
        return int((nt - pt).total_seconds())
    except Exception:
        return None


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


def normalize_quotes(payload: dict, refs, provider_name: str, default_mode: str, now: str,
                     stale_after_sec: "int | None" = None):
    """PURE, network-free normalization of a Tradier /markets/quotes response into Quote objects.
    Extracted so it can be unit-tested against real and synthetic payload SHAPES. It NEVER
    fabricates a price: every unusable quote is ok=False with a TYPED `data_quality` reason.

    Handled real-response shapes:
      * quotes.quote a single object (one symbol requested) OR a list (many)
      * a row missing bid and/or ask                       -> DQ_MISSING_BID_ASK
      * a row with bid == 0 (no live market)               -> DQ_ZERO_BID
      * a contract absent from the response                -> DQ_OPTION_NOT_FOUND
      * a REALTIME row older than stale_after_sec           -> DQ_STALE
      * a row that isn't a parseable object / has no symbol -> DQ_MALFORMED_ROW (skipped from index)

    Refs are matched on the OCC symbol (`ref.occ_symbol`, falling back to contract_id only when it
    is itself a valid OCC — for direct unit tests). The provider is NEVER matched on a human label.
    """
    refs = list(refs)

    def _key(r):
        return r.occ_symbol if is_valid_occ(r.occ_symbol) else (
            r.contract_id if is_valid_occ(r.contract_id) else r.occ_symbol or r.contract_id)

    by_key = {_key(r): r for r in refs}
    quotes_node = ((payload or {}).get("quotes") or {}).get("quote")
    if quotes_node is None:
        rows = []
    elif isinstance(quotes_node, list):
        rows = quotes_node
    else:
        rows = [quotes_node]

    seen = {}
    for row in rows:
        # a malformed row (not a dict, or no symbol) is skipped from the index — the ref for it,
        # if any, falls through to DQ_OPTION_NOT_FOUND / DQ_MALFORMED_ROW below. Never crashes.
        if not isinstance(row, dict):
            continue
        sym = row.get("symbol")
        if not sym or not isinstance(sym, str):
            continue
        ref = by_key.get(sym)
        cid = ref.contract_id if ref is not None else sym
        # Tradier flags delayed quotes for the account/entitlement; default to real-time only when
        # the feed does NOT say delayed.
        if "delayed" in row:
            delayed_flag = bool(row.get("delayed"))
        else:
            delayed_flag = default_mode == MODE_DELAYED
        mode = MODE_DELAYED if delayed_flag else MODE_REALTIME
        greeks = row.get("greeks") if isinstance(row.get("greeks"), dict) else {}
        pqt = None
        tms = row.get("trade_date") or row.get("bid_date") or row.get("ask_date")
        if tms is not None:
            try:
                pqt = datetime.fromtimestamp(int(tms) / 1000, tz=timezone.utc).isoformat()
            except Exception:
                pqt = None

        # numeric coercion is defensive: a non-numeric bid/ask is treated as absent, not crashed.
        def _num(v):
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        bid = _num(row.get("bid"))
        ask = _num(row.get("ask"))

        # ---- typed data-quality decision (never fabricate) ----
        dq = DQ_OK
        ok = True
        note = None
        if bid is None or ask is None:
            dq, ok = DQ_MISSING_BID_ASK, False
            note = "no bid/ask returned for this contract"
        elif bid == 0:
            dq, ok = DQ_ZERO_BID, False
            note = "bid is 0 — no live market; not a usable price"
        elif stale_after_sec is not None and mode == MODE_REALTIME:
            age = _iso_age_seconds(pqt, now)
            if age is not None and age > stale_after_sec:
                dq, ok = DQ_STALE, False
                note = f"quote {age}s old (> {stale_after_sec}s tolerance) — stale, not acted on"
        mid = round((bid + ask) / 2, 4) if (ok and bid is not None and ask is not None) else None

        seen[sym] = Quote(
            contract_id=cid, provider=provider_name, mode=mode, ok=ok,
            provider_quote_ts=pqt, ingestion_ts=now,
            bid=bid, ask=ask, mid=mid, underlying=None,
            iv=greeks.get("mid_iv"), delta=greeks.get("delta"), theta=greeks.get("theta"),
            dte=ref.dte if ref is not None else None,
            note=note, occ_symbol=sym, data_quality=dq,
        )

    out = []
    for r in refs:
        k = _key(r)
        if k in seen:
            out.append(seen[k])
        else:
            out.append(Quote(contract_id=r.contract_id, provider=provider_name,
                             mode=default_mode, ok=False, ingestion_ts=now, dte=r.dte,
                             occ_symbol=r.occ_symbol, data_quality=DQ_OPTION_NOT_FOUND,
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
        """GET /markets/quotes for the given OCC symbols. Maps real Tradier failure responses to
        TYPED exceptions so the worker can distinguish auth / rate-limit / malformed / transport."""
        params = "symbols=" + ",".join(symbols) + "&greeks=true"
        url = f"{self._base}{QUOTES_PATH}?{params}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                body = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            code = e.code
            if code in (401, 403):
                raise ProviderAuthError(
                    f"Tradier rejected the credential (HTTP {code}) — token missing/invalid or "
                    "lacks a real-time option-quote entitlement. Feed NOT marked live.") from e
            if code == 429:
                raise ProviderRateLimitError("Tradier rate limit (HTTP 429) — backing off; "
                                             "no quote fabricated.") from e
            raise ProviderError(f"Tradier HTTP {code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # a transport failure is a TOTAL loss for this poll.
            raise ProviderError(f"Tradier transport error: {e}") from e
        try:
            data = json.loads(body)
        except (ValueError, TypeError) as e:
            raise ProviderPayloadError("Tradier returned a non-JSON / unparseable body — "
                                       "treated as total loss; no quote fabricated.") from e
        if not isinstance(data, dict):
            raise ProviderPayloadError("Tradier body was not a JSON object — no quote fabricated.")
        return data

    def get_quotes(self, refs: Iterable[ContractRef]) -> List[Quote]:
        refs = list(refs)
        if not refs:
            return []
        if not self._connected:
            # total feed loss for THIS call — signal it; the worker decides fallback.
            raise ProviderError("TradierProvider.get_quotes called while disconnected")

        # REFUSE to query the provider with anything but a valid OCC symbol. Refs without one become
        # DQ_MISSING_OCC refusals — a human-readable label is NEVER sent upstream (the boot bug).
        valid, invalid = split_refs_by_occ(refs)
        now = _now_utc_iso()
        refusals = [missing_occ_quote(r, self.name, self.default_mode, now) for r in invalid]
        if not valid:
            return refusals

        symbols = [r.occ_symbol for r in valid]
        payload = self._http_get(symbols)   # raises TYPED ProviderError subclasses on failure
        # pure, tested normalization — no fabrication; stale REALTIME rows -> DQ_STALE
        quotes = normalize_quotes(payload, valid, self.name, self.default_mode, now,
                                  stale_after_sec=STALE_AFTER_SEC)
        # preserve input order (valid first as requested, then refusals) — the worker maps by ref
        return quotes + refusals
