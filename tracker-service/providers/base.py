"""
Provider abstraction for the real-time active-position tracker (Phase 2).

A Provider is a NARROW interface: given a set of OCC option contracts that have
OPEN paper positions, return the current quote for each. Nothing here knows about
stops, trailing, or the research filter — the worker owns all of that and reuses
the *frozen* engine policy so simulation logic never forks between phases.

Design invariants (enforced by tests):
  * A provider NEVER fabricates a quote. If it has no fresh data for a contract,
    it returns a Quote with `ok=False` and NO bid/ask — never a guessed value.
  * A provider stamps its own quote timestamp (`provider_quote_ts`) from the feed,
    plus the ingestion time (`ingestion_ts`) recorded by the worker. The worker,
    not the provider, computes observed lag from those two.
  * `mode` is REALTIME or DELAYED. A real-time provider that has fallen back to a
    delayed source labels those quotes DELAYED (or DELAYED_FALLBACK) — it must not
    pass stale/delayed data off as real-time.
  * Providers are subscribed ONLY to contracts with open paper positions. They must
    not stream or poll the entire options market.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field, asdict
from typing import Optional, Iterable, List, Dict, Any

# provider modes
MODE_REALTIME = "REALTIME"
MODE_DELAYED = "DELAYED"
MODE_DELAYED_FALLBACK = "DELAYED_FALLBACK"

# feed-health / connection statuses used across the tracker
LIVE = "LIVE"
DELAYED = "DELAYED"
STALE = "STALE"
DISCONNECTED = "DISCONNECTED"
FALLBACK_TO_CBOE = "FALLBACK_TO_CBOE"   # canonical status when the primary feed falls to the delayed floor
EXIT_FIRST_OBSERVED = "EXIT_FIRST_OBSERVED"
SIMULATED_CLOSED = "SIMULATED_CLOSED"


# ------------------------------------------------------------------ OCC symbol validation
# The tracker subscribes to the provider on the OCC option symbol, NOT the human-readable
# contract label. An OCC symbol is: root (1-6 letters) + YYMMDD + C|P + 8-digit strike
# (strike*1000, zero-padded). e.g. DIS261016P00105000 -> DIS 2026-10-16 put 105.0.
OCC_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")


def is_valid_occ(sym: Optional[str]) -> bool:
    """True iff `sym` is a well-formed OCC option symbol. Used to REFUSE a quote request
    rather than send the provider a human-readable label (the Phase 2 boot bug)."""
    return bool(sym) and bool(OCC_RE.match(sym))


# ------------------------------------------------------------------ data-quality reasons (typed)
# A missing / unusable quote is NEVER fabricated into a midpoint. It is returned as ok=False
# with one of these TYPED data_quality reasons so the worker records WHY, precisely.
DQ_OK = "OK"
DQ_MISSING_BID_ASK = "MISSING_BID_ASK"      # row present but no bid and/or no ask
DQ_ZERO_BID = "ZERO_BID"                    # bid is 0 (no live market) — not a real price
DQ_OPTION_NOT_FOUND = "OPTION_NOT_FOUND"    # contract absent from the provider response
DQ_STALE = "STALE"                          # quote timestamp older than tolerated
DQ_MALFORMED_ROW = "MALFORMED_ROW"          # row could not be parsed into a quote
DQ_MISSING_OCC = "MISSING_OCC"              # ref carried no valid OCC symbol — never requested


# ------------------------------------------------------------------ typed provider exceptions
class ProviderError(Exception):
    """Base for a TOTAL feed failure on one poll (the worker marks DISCONNECTED / may fall back).
    Distinct from a single unusable contract, which is ok=False with a data_quality reason."""


class ProviderAuthError(ProviderError):
    """The provider rejected the credential (HTTP 401/403). The token is missing/invalid/lacks
    entitlement. The worker must NOT mark the feed LIVE and must not retry-fabricate."""


class ProviderRateLimitError(ProviderError):
    """The provider rate-limited this poll (HTTP 429). Transient; back off — never fabricate."""


class ProviderPayloadError(ProviderError):
    """The provider returned a non-JSON / structurally invalid body. Treated as a total loss for
    this poll; no quotes are fabricated from an unparseable payload."""


# ------------------------------------------------------------------ provider registry
# Adapters SELF-REGISTER with @register("name"). The worker/config resolves a provider by
# name through this registry and never imports a concrete adapter — so adding a broker means
# adding ONE adapter file that registers itself; no factory, worker, or engine code changes.
PROVIDERS: "Dict[str, type]" = {}


def register(name: str):
    """Class decorator: register a Provider subclass under a config name (lowercased)."""
    def _wrap(cls):
        key = name.lower()
        if key in PROVIDERS and PROVIDERS[key] is not cls:
            raise ValueError(f"provider name '{key}' already registered to {PROVIDERS[key].__name__}")
        cls.name = key
        PROVIDERS[key] = cls
        return cls
    return _wrap


def available_providers() -> "List[str]":
    """Names the tracker can be pointed at by config. Includes not-yet-implemented stubs."""
    return sorted(PROVIDERS.keys())


def create_provider(name: str, **kwargs) -> "Provider":
    """Instantiate a registered provider by config name. Raises if the name is unknown —
    it NEVER silently substitutes a different provider."""
    key = (name or "").lower()
    if key not in PROVIDERS:
        raise ValueError(f"unknown provider '{name}' (registered: {', '.join(available_providers())})")
    return PROVIDERS[key](**kwargs)


@dataclass
class ContractRef:
    """The minimal identity the tracker subscribes on. Sourced from an open paper
    position in the research watchlist — never invented.

    TWO identifiers are preserved and NOT interchangeable:
      * `contract_id` — the HUMAN-READABLE label ("KO 90C 2026-10-16"). Display only.
      * `occ_symbol`  — the OCC option symbol ("KO261016C00090000"). This, and ONLY this,
        is what a provider is queried with. The Phase 2 boot bug was sending contract_id
        (the human label) as the provider symbol; providers must key on occ_symbol."""
    paper_position_id: str
    contract_id: str          # HUMAN-READABLE label, e.g. "KO 90C 2026-10-16" — display only
    symbol: str
    right: str                # "call" | "put"
    strike: float
    expiration: str           # YYYY-MM-DD
    dte: Optional[int] = None
    occ_symbol: Optional[str] = None   # OCC symbol, e.g. KO261016C00090000 — the PROVIDER key

    def has_valid_occ(self) -> bool:
        return is_valid_occ(self.occ_symbol)


@dataclass
class Quote:
    """A single observation for one contract. Raw and preserved verbatim by the
    worker's append-only event log; the worker never rewrites a stored Quote."""
    contract_id: str                  # HUMAN-READABLE label carried through for display
    provider: str
    mode: str                         # MODE_REALTIME | MODE_DELAYED | MODE_DELAYED_FALLBACK
    ok: bool                          # False => no usable price; bid/ask stay None
    provider_quote_ts: Optional[str] = None   # feed's own timestamp (ISO)
    ingestion_ts: Optional[str] = None        # set by the worker when received (ISO, UTC)
    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None
    underlying: Optional[float] = None
    iv: Optional[float] = None
    delta: Optional[float] = None
    theta: Optional[float] = None
    dte: Optional[int] = None
    note: Optional[str] = None
    occ_symbol: Optional[str] = None          # the OCC symbol this quote answers (provider key)
    data_quality: Optional[str] = None        # DQ_* reason; DQ_OK when ok=True, a TYPED reason when not

    def to_res(self) -> Dict[str, Any]:
        """Adapt to the shape the engine's paper simulation consumes (_paper_observe).
        `mark` is the engine's name for mid. `passed` is unknown to a real-time feed
        (that's a research-filter concept), so it is left False — the tracker never
        claims filter membership from a price quote."""
        mid = self.mid
        if mid is None and self.bid is not None and self.ask is not None:
            mid = round((self.bid + self.ask) / 2, 4)
        return {
            "bid": self.bid, "ask": self.ask, "mark": mid,
            "underlying": self.underlying, "dte": self.dte, "passed": False,
            "provider": self.provider, "provider_mode": self.mode,
            "provider_quote_ts": self.provider_quote_ts,
            "iv": self.iv, "delta": self.delta, "theta": self.theta,
        }

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def missing_occ_quote(ref: "ContractRef", provider_name: str, default_mode: str,
                      now: Optional[str] = None) -> "Quote":
    """A REFUSAL quote for a ref without a valid OCC symbol. The provider never sends a
    human-readable label upstream; it returns ok=False / DQ_MISSING_OCC instead of guessing."""
    return Quote(contract_id=ref.contract_id, provider=provider_name, mode=default_mode,
                 ok=False, ingestion_ts=now, dte=ref.dte, occ_symbol=ref.occ_symbol,
                 data_quality=DQ_MISSING_OCC,
                 note="ref carried no valid OCC symbol — quote request refused, nothing sent upstream")


def split_refs_by_occ(refs: "Iterable[ContractRef]"):
    """Partition refs into (valid_occ, invalid_occ). Only valid_occ symbols are ever sent to a
    provider; invalid_occ become DQ_MISSING_OCC refusals — never queried with a display label."""
    valid, invalid = [], []
    for r in refs:
        (valid if is_valid_occ(r.occ_symbol) else invalid).append(r)
    return valid, invalid


class Provider:
    """Abstract base. Concrete adapters (Tradier real-time, CBOE delayed fallback)
    implement get_quotes(). Nothing here holds a socket open inside GitHub Actions —
    this runs in a PERSISTENT worker process (see README)."""

    name = "base"
    default_mode = MODE_REALTIME

    def __init__(self, **kwargs):
        self._connected = False

    # ---- lifecycle -------------------------------------------------------
    def connect(self) -> None:
        """Establish the session / validate credentials. Raise on hard failure so the
        worker can mark DISCONNECTED and (optionally) fall back — never swallow it into
        a fake 'connected'."""
        self._connected = True

    def close(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ---- the one real method --------------------------------------------
    def get_quotes(self, refs: Iterable[ContractRef]) -> List[Quote]:
        """Return one Quote per ContractRef. Implementations MUST:
          * request ONLY the given contracts (open paper positions),
          * return ok=False (no bid/ask) for any contract without fresh data,
          * never raise for a single missing contract (raise only on total feed loss).
        """
        raise NotImplementedError

    # ---- readiness / health ---------------------------------------------
    def configured(self) -> bool:
        """Whether this provider has everything it needs to operate (e.g. its secret). Adapters
        that require a token override this; providers with no credential are always configured.
        The worker uses this to stay dormant/NOT_CONFIGURED instead of requesting quotes."""
        return True

    def heartbeat(self) -> bool:
        """Cheap liveness probe. Default: are we connected. Adapters may ping the API."""
        return self._connected
