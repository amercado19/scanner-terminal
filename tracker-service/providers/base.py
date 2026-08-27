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
    position in the research watchlist — never invented."""
    paper_position_id: str
    contract_id: str          # OCC symbol, e.g. KO261016C00090000
    symbol: str
    right: str                # "call" | "put"
    strike: float
    expiration: str           # YYYY-MM-DD
    dte: Optional[int] = None


@dataclass
class Quote:
    """A single observation for one contract. Raw and preserved verbatim by the
    worker's append-only event log; the worker never rewrites a stored Quote."""
    contract_id: str
    provider: str
    mode: str                         # MODE_REALTIME | MODE_DELAYED | MODE_DELAYED_FALLBACK
    ok: bool                          # False => no fresh data; bid/ask stay None
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
