#!/usr/bin/env python3
"""
Real-time active-position tracker — PERSISTENT WORKER (Phase 2).

    STATUS: SCAFFOLD — NOT OPERATIONAL.
    This worker is fully built and unit-tested against a mock provider, but it is NOT
    running against a live real-time feed. Turning it on requires a provider account,
    one secret, and a persistent host (see README.md → "Going live"). Until then, every
    open paper position is tracked by the Phase 1 GitHub Actions scanner on CBOE DELAYED
    data, which is labelled as delayed everywhere.

What it does when operational:
  * Loads the OPEN paper positions from the Phase 1 research watchlist (read-only).
  * Subscribes/polls the real-time provider for ONLY those contracts (never the whole market).
  * On each poll: records a raw observation, updates high-water mark + trailing stop, and
    evaluates the SAME frozen stop ladder as Phase 1 (imported from the engine — policy
    never forks). When an exit condition is FIRST OBSERVED, it creates a simulated exit at
    the observed BID (conservative) — it does NOT reconstruct a fill at the stop price.
  * Writes an APPEND-ONLY event log. Prior events are never rewritten. Missing quotes are
    never fabricated.
  * Tracks feed health (LIVE / DELAYED / STALE / DISCONNECTED), heartbeats, and — on provider
    failure — optionally FALLS BACK to CBOE delayed, labelled DELAYED_FALLBACK, without mixing
    timestamps or overwriting real-time history. On recovery it appends a recovery event and
    resumes; it does not backfill the gap.

Separation of concerns: the worker does NOT write back into the Phase 1 watchlist. It maintains
its own event store (its own output repo / GitHub Pages), keyed by paper_position_id. This keeps
the delayed-discovery lifecycle and the real-time-tracking lifecycle as independent, append-only
records that never race each other.
"""
from __future__ import annotations
import os
import sys
import json
import time
import signal
import logging
import argparse
import threading
import importlib.util
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from providers import (  # noqa: E402
    get_provider, create_provider, available_providers, Quote, ContractRef,
    MODE_REALTIME, MODE_DELAYED, MODE_DELAYED_FALLBACK,
    LIVE, DELAYED, STALE, DISCONNECTED,
    FALLBACK_TO_CBOE, EXIT_FIRST_OBSERVED, SIMULATED_CLOSED,
)
import config as trackercfg  # noqa: E402

SCHEMA_VERSION = "tracker-service.v0.2"
ANALYTICS = "TRADE_ANALYTICS"
# the honest delayed floor used as the DEFAULT fallback (itself just another adapter, swappable
# by config). This is the ONLY provider name the worker module references, and only as the floor.
DEFAULT_FALLBACK_NAME = "cboe"

# worker lifecycle statuses (distinct from feed health)
NOT_CONFIGURED = "NOT_CONFIGURED"   # no primary credential -> dormant, never requests live quotes
RUNNING = "RUNNING"                 # polling the configured provider
DEGRADED = "DEGRADED"               # provider disconnected / on fallback
SHUTTING_DOWN = "SHUTTING_DOWN"

# ------------------------------------------------------------------ structured logging
_log = logging.getLogger("tracker")


def _setup_logging():
    if _log.handlers:
        return
    h = logging.StreamHandler(sys.stdout)

    class _JsonFmt(logging.Formatter):
        def format(self, rec):
            payload = {"level": rec.levelname, "logger": rec.name, "msg": rec.getMessage()}
            if isinstance(rec.args, dict):
                payload.update(rec.args)
            extra = getattr(rec, "fields", None)
            if isinstance(extra, dict):
                payload.update(extra)
            return json.dumps(payload, separators=(",", ":"))

    h.setFormatter(_JsonFmt())
    _log.addHandler(h)
    _log.setLevel(logging.INFO)


def log(msg, **fields):
    _setup_logging()
    _log.info(msg, extra={"fields": fields})
# lag beyond which a REALTIME quote is treated as STALE (not fresh enough to act on)
STALE_LAG_SEC = 120


# ------------------------------------------------------------------ engine import
def load_engine(explicit: Optional[str] = None):
    """Import the Phase 1 engine so the tracker reuses its FROZEN policy + stop ladder.
    Searched paths keep policy single-source; the worker never re-implements stops."""
    candidates = [explicit] if explicit else []
    here = os.path.dirname(os.path.abspath(__file__))
    candidates += [
        os.path.join(here, "..", "research-scanner", "research_scanner.py"),
        os.path.join(here, "..", "research_scanner.py"),
        os.path.join(here, "research_scanner.py"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            spec = importlib.util.spec_from_file_location("research_scanner", c)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError(
        "research_scanner.py (Phase 1 engine) not found — the tracker refuses to run with a "
        "forked copy of the stop policy. Pass --engine PATH."
    )


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lag_seconds(provider_ts: Optional[str], now_iso: str) -> Optional[int]:
    """Delegates to the engine's lag math if available; else best-effort. Never fabricates."""
    if not provider_ts or not now_iso:
        return None
    try:
        from datetime import datetime as _dt
        try:
            from zoneinfo import ZoneInfo
            # naive provider stamps (CBOE) are US/Eastern; ISO stamps with tz keep theirs
            if provider_ts.endswith("Z") or "+" in provider_ts[10:] or provider_ts[10:].count("-") > 0:
                pt = _dt.fromisoformat(provider_ts.replace("Z", "+00:00"))
            else:
                pt = _dt.fromisoformat(provider_ts).replace(tzinfo=ZoneInfo("America/New_York"))
        except Exception:
            pt = _dt.fromisoformat(provider_ts.replace("Z", "+00:00"))
        nt = _dt.fromisoformat(now_iso.replace("Z", "+00:00"))
        if nt.tzinfo is None:
            nt = nt.replace(tzinfo=timezone.utc)
        if pt.tzinfo is None:
            pt = pt.replace(tzinfo=timezone.utc)
        return int((nt - pt).total_seconds())
    except Exception:
        return None


def feed_health(quote: Optional[Quote], now_iso: str) -> str:
    """Feed-health axis, independent of the paper result."""
    if quote is None or not quote.ok:
        return DISCONNECTED if quote is None else STALE
    if quote.mode in (MODE_DELAYED, MODE_DELAYED_FALLBACK):
        return DELAYED
    lag = _lag_seconds(quote.provider_quote_ts, now_iso)
    if lag is None:
        return LIVE  # real-time provider with no stamp; treat as live but note absence elsewhere
    return LIVE if lag <= STALE_LAG_SEC else STALE


# ------------------------------------------------------------------ position loading
def _read_watchlist(watchlist_path: str) -> Dict[str, Any]:
    """Read the Phase 1 watchlist from a local path OR an http(s) URL. On Railway the current
    watchlist lives in the repo (updated every ~15 min by Actions), so the worker points at the
    raw URL to always see the latest OPEN positions rather than a stale build-time copy."""
    if watchlist_path.startswith(("http://", "https://")):
        import urllib.request
        req = urllib.request.Request(watchlist_path, headers={"User-Agent": "scanner-terminal-tracker"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    with open(watchlist_path, encoding="utf-8") as f:
        return json.load(f)


def load_open_positions(watchlist_path: str) -> List[Dict[str, Any]]:
    """Read (never write) the Phase 1 watchlist; return the cards whose paper position is OPEN.
    An OPEN position is tracked EVEN IF its research_status is LEFT_FILTER/EXPIRED — leaving the
    research filter never stops paper tracking."""
    data = _read_watchlist(watchlist_path)
    active = (data.get("active") or {})
    out = []
    for cid, card in active.items():
        pp = card.get("paper_position") or {}
        if pp.get("status") in ("ACTIVE", "TRAILING_ACTIVE"):
            out.append(card)
    return out


def refs_from_positions(cards: List[Dict[str, Any]]) -> List[ContractRef]:
    refs = []
    for card in cards:
        pp = card.get("paper_position") or {}
        refs.append(ContractRef(
            paper_position_id=pp.get("id") or card.get("contract_id"),
            contract_id=card.get("contract_id"),
            symbol=card.get("symbol"),
            right=card.get("right"),
            strike=card.get("strike"),
            expiration=card.get("expiration"),
            dte=pp.get("current_dte") or card.get("dte"),
        ))
    return refs


# ------------------------------------------------------------------ descriptive trade analytics
def _initial_stop_counterfactual_pct(mirror: Dict[str, Any]) -> Optional[float]:
    """Descriptive counterfactual: what return the INITIAL stop alone would have produced on the
    SAME observed price path. Two deterministic rules replayed over recorded observations — NOT a
    prediction. If the path never hit the initial stop, the endpoint is the final observed return."""
    entry = mirror.get("entry_mid")
    init_level = mirror.get("initial_stop_level")
    if not entry or init_level is None:
        return None
    for o in mirror.get("observations", []):
        mid = o.get("mid")
        if mid is not None and mid <= init_level:
            bid = o.get("bid")
            exit_px = bid if bid is not None else mid   # conservative, same rule as live exits
            return round((exit_px - entry) / entry * 100, 2)
    return mirror.get("current_pct")


def build_trade_analytics(mirror: Dict[str, Any], card: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """A DESCRIPTIVE record of one completed paper trade. Measured facts + a deterministic
    rule-vs-rule comparison. NO scores, ranks, expected return, probability, recommendation, or
    predictive model — only what happened on the observed path."""
    obs = mirror.get("observations", [])
    pp = card.get("paper_position") or {}
    entry_ts = pp.get("entry_ts") or pp.get("entered_at") or (obs[0]["ts"] if obs else None)
    exit_ts = obs[-1]["ts"] if obs else _now_utc_iso()
    hold_sec = _lag_seconds(entry_ts, exit_ts) if (entry_ts and exit_ts) else None
    final_ret = mirror.get("current_pct")
    init_cf = _initial_stop_counterfactual_pct(mirror)
    trailing_delta = (round(final_ret - init_cf, 2)
                      if (final_ret is not None and init_cf is not None) else None)
    # research_status is owned by the Phase 1 discovery engine; the tracker only reports what the
    # card said at close — it does not infer filter membership from price.
    research_status = card.get("research_status") or card.get("current_status")
    left_filter = research_status in ("LEFT_FILTER", "EXPIRED") if research_status else None
    return {
        "event": ANALYTICS,
        "position_id": mirror.get("id"),
        "contract_id": card.get("contract_id"),
        "exit_reason": reason,
        # timestamps + holding time
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "holding_time_sec": hold_sec,
        "holding_time_days": (round(hold_sec / 86400, 3) if hold_sec is not None else None),
        "observed_trading_days_held": len({o["ts"][:10] for o in obs if o.get("ts")}) or None,
        # excursions (percent, relative to paper entry)
        "mfe_pct": mirror.get("mfe"),
        "mae_pct": mirror.get("mae"),
        "peak_profit_pct": mirror.get("mfe"),      # max favorable unrealized gain on the path
        "peak_drawdown_pct": mirror.get("mae"),    # max adverse unrealized loss on the path
        "final_return_pct": final_ret,
        # option value extremes (price)
        "entry_mid": mirror.get("entry_mid"),
        "exit_price_observed_bid": mirror.get("exit_price"),
        "highest_option_value": mirror.get("highest_mid"),
        "lowest_option_value": mirror.get("lowest_mid"),
        # underlying extremes (both raw and % move relative to entry underlying)
        "highest_underlying": mirror.get("highest_underlying"),
        "lowest_underlying": mirror.get("lowest_underlying"),
        "highest_underlying_move_pct": mirror.get("highest_underlying_pct"),
        "lowest_underlying_move_pct": mirror.get("lowest_underlying_pct"),
        # deterministic rule comparison (descriptive, not predictive)
        "initial_stop_only_return_pct": init_cf,
        "trailing_vs_initial_delta_pct": trailing_delta,
        "trailing_improved_result": (trailing_delta > 0) if trailing_delta is not None else None,
        "trailing_was_active": bool(mirror.get("trailing_active")),
        # lifecycle context (owned by the discovery engine; reported, not inferred)
        "research_status_at_close": research_status,
        "left_research_filter_before_close": left_filter,
        # honesty guard
        "descriptive_only": True,
        "note": ("Descriptive statistics of one simulated paper trade on its observed path. "
                 "No score, rank, expected return, probability, recommendation, or prediction."),
    }


# ------------------------------------------------------------------ event store (append-only)
class EventStore:
    """Append-only JSONL of raw observations + lifecycle events, one file per position.
    Prior lines are NEVER rewritten. This is the tracker's durable output (its own repo /
    Pages), distinct from the Phase 1 watchlist."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, position_id: str) -> str:
        safe = "".join(c for c in position_id if c.isalnum() or c in "-_.")
        return os.path.join(self.root, f"{safe}.jsonl")

    def append(self, position_id: str, event: Dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("schema_version", SCHEMA_VERSION)
        with open(self._path(position_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")

    def read(self, position_id: str) -> List[Dict[str, Any]]:
        p = self._path(position_id)
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def is_closed(self, position_id: str) -> bool:
        return any(e.get("event") == SIMULATED_CLOSED for e in self.read(position_id))


# ------------------------------------------------------------------ worker state (published)
def _write_json_atomic(path: str, obj: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


class WorkerState:
    """The live, published status of the worker — the single source the dashboard reads to show
    provider mode, badges, timestamps, latency, heartbeat, and counts. `ever_received_realtime`
    gates the LIVE badge: the dashboard must NOT show LIVE until a real real-time quote arrived."""

    def __init__(self, provider_name: Optional[str], fallback_name: Optional[str]):
        self.status = NOT_CONFIGURED
        self.provider = provider_name
        self.fallback = fallback_name
        self.provider_mode = None            # REALTIME | DELAYED | DELAYED_FALLBACK | None
        self.on_fallback = False
        self.heartbeat_ts = None
        self.last_realtime_quote_ts = None   # last successful REAL real-time quote (gates LIVE)
        self.last_delayed_quote_ts = None
        self.ever_received_realtime = False
        self.open_positions = 0
        self.receiving_realtime = 0
        self.on_delayed_fallback = 0
        self.stale = 0
        self.disconnected = 0
        self.last_error = None
        self.started_ts = _now_utc_iso()

    def badge(self) -> str:
        """Dashboard badge. LIVE only after a real real-time quote; otherwise DELAYED/FALLBACK/—."""
        if self.status == NOT_CONFIGURED:
            return NOT_CONFIGURED
        if self.on_fallback:
            return "FALLBACK"
        if self.ever_received_realtime and self.provider_mode == MODE_REALTIME:
            return "REALTIME"
        if self.provider_mode in (MODE_DELAYED, MODE_DELAYED_FALLBACK):
            return "DELAYED"
        return "DELAYED"

    def snapshot(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "worker_status": self.status,
            "primary_provider": self.provider,
            "fallback_provider": self.fallback,
            "provider_mode": self.provider_mode,
            "badge": self.badge(),
            "on_fallback": self.on_fallback,
            "heartbeat_ts": self.heartbeat_ts,
            "last_realtime_quote_ts": self.last_realtime_quote_ts,
            "last_delayed_quote_ts": self.last_delayed_quote_ts,
            "ever_received_realtime": self.ever_received_realtime,
            "active_positions": self.open_positions,
            "receiving_realtime": self.receiving_realtime,
            "on_delayed_fallback": self.on_delayed_fallback,
            "stale": self.stale,
            "disconnected": self.disconnected,
            "last_error": self.last_error,
            "started_ts": self.started_ts,
            "published_ts": _now_utc_iso(),
            "note": ("Phase 2 real-time tracker state. LIVE badge appears only after a real "
                     "real-time quote; delayed/fallback data is always labelled."),
        }


# ------------------------------------------------------------------ the worker
class Tracker:
    def __init__(self, engine, watchlist_path: str, store: EventStore,
                 provider=None, provider_name: Optional[str] = None, allow_fallback: bool = True,
                 provider_kwargs: Optional[dict] = None, fallback=None, state_dir: Optional[str] = None):
        """The worker is PROVIDER-AGNOSTIC. It takes a provider INSTANCE (or a name to resolve
        through the registry) and thereafter references only `self.provider` / `self.fallback` —
        it never branches on which broker it is. Swapping providers is a config change; this
        class does not change."""
        self.engine = engine
        self.policy = dict(getattr(engine, "DEFAULT_POLICY"))
        self.watchlist_path = watchlist_path
        self.store = store
        self.allow_fallback = allow_fallback
        # resolve the PRIMARY provider from an injected instance or a config name (registry lookup)
        if provider is None:
            if not provider_name:
                raise ValueError("Tracker needs a provider instance or a provider name (from config)")
            provider = create_provider(provider_name, **(provider_kwargs or {}))
        self.provider = provider
        self.provider_name = getattr(provider, "name", None)   # derived from the instance, not hard-coded
        # fallback is an INSTANCE too; default is the honest delayed floor, itself swappable
        if fallback is not None:
            self.fallback = fallback
        elif allow_fallback:
            self.fallback = create_provider(DEFAULT_FALLBACK_NAME)
        else:
            self.fallback = None
        self.fallback_name = getattr(self.fallback, "name", None)
        self._pp_state: Dict[str, Dict[str, Any]] = {}   # position_id -> engine paper_position mirror
        self.last_realtime_update: Optional[str] = None
        self.last_delayed_update: Optional[str] = None
        self.on_fallback = False
        # persistent-volume root for published state/heartbeat/health/checkpoint (Railway volume);
        # the EventStore itself is passed in (main() roots it under the same volume).
        self.state_dir = state_dir
        # readiness: a provider that needs a secret reports configured()==False without it
        self.is_configured = bool(getattr(self.provider, "configured", lambda: True)())
        self.state = WorkerState(self.provider_name, self.fallback_name)
        if not self.is_configured:
            self.state.status = NOT_CONFIGURED
        self._publish()   # publish an initial (likely NOT_CONFIGURED) state at construction

    # -- rehydrate a per-position engine paper_position mirror from the Phase 1 card --
    def _mirror(self, card: Dict[str, Any]) -> Dict[str, Any]:
        pp = card.get("paper_position") or {}
        pid = pp.get("id") or card.get("contract_id")
        if pid in self._pp_state:
            return self._pp_state[pid]
        # seed a fresh engine paper_position with the SAME params + entry, empty obs (the
        # tracker builds its own real-time observation history; it never copies delayed obs in)
        mirror = {
            "id": pid,
            "status": pp.get("status", "ACTIVE"),
            "params": pp.get("params") or dict(self.policy),
            "policy_version": pp.get("policy_version"),
            "entry_mid": pp.get("entry_mid"),
            "entry_underlying": pp.get("entry_underlying"),
            "observations": [],
            "trailing_active": pp.get("trailing_active", False),
            "trailing_high": pp.get("trailing_high"),
            "trailing_stop_level": pp.get("trailing_stop_level"),
            "initial_stop_level": pp.get("initial_stop_level"),
            "current_stop_level": pp.get("current_stop_level"),
        }
        self._pp_state[pid] = mirror
        return mirror

    def heartbeat(self) -> Dict[str, Any]:
        return {
            "event": "HEARTBEAT", "ts": _now_utc_iso(),
            "provider": self.provider_name,
            "provider_connected": bool(self.provider.connected),
            "on_fallback": self.on_fallback,
            "last_realtime_update": self.last_realtime_update,
            "last_delayed_update": self.last_delayed_update,
        }

    # ---- persistent-volume outputs (survive container restarts) ----------
    def _publish(self) -> None:
        """Write the dashboard-facing worker_state.json + heartbeat.json to the volume."""
        self.state.heartbeat_ts = _now_utc_iso()
        if not self.state_dir:
            return
        try:
            _write_json_atomic(os.path.join(self.state_dir, "worker_state.json"), self.state.snapshot())
            _write_json_atomic(os.path.join(self.state_dir, "heartbeat.json"),
                               {"ts": self.state.heartbeat_ts, "status": self.state.status,
                                "badge": self.state.badge()})
        except Exception as e:
            log("publish_failed", error=str(e))

    def _record_provider_health(self, kind: str, detail: Optional[str] = None) -> None:
        """Append one provider-health transition to provider_health.jsonl on the volume."""
        rec = {"ts": _now_utc_iso(), "kind": kind, "provider": self.provider_name,
               "on_fallback": self.on_fallback, "detail": detail}
        if self.state_dir:
            try:
                with open(os.path.join(self.state_dir, "provider_health.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            except Exception as e:
                log("provider_health_write_failed", error=str(e))

    def _checkpoint(self) -> None:
        """Recovery checkpoint: enough per-position mirror state to resume after a restart without
        losing high-water marks / trailing levels. Real-time observation HISTORY lives in the
        append-only event store; this is the resumable summary."""
        if not self.state_dir:
            return
        ck = {"ts": _now_utc_iso(), "on_fallback": self.on_fallback,
              "last_realtime_update": self.last_realtime_update,
              "last_delayed_update": self.last_delayed_update,
              "positions": {pid: {k: m.get(k) for k in
                                  ("status", "entry_mid", "trailing_active", "trailing_high",
                                   "trailing_stop_level", "initial_stop_level", "current_stop_level",
                                   "highest_mid", "lowest_mid", "current_pct", "policy_version")}
                            for pid, m in self._pp_state.items()}}
        try:
            _write_json_atomic(os.path.join(self.state_dir, "checkpoint.json"), ck)
        except Exception as e:
            log("checkpoint_write_failed", error=str(e))

    def _acquire_quotes(self, refs: List[ContractRef]) -> tuple[List[Quote], bool]:
        """Try the real-time provider; on TOTAL failure optionally fall back to CBOE delayed.
        Returns (quotes, used_fallback). Never fabricates on double failure — returns []."""
        try:
            if not self.provider.connected:
                self.provider.connect()
            quotes = self.provider.get_quotes(refs)
            if self.on_fallback:
                # we were on fallback and real-time is back: append a recovery marker per position
                self.on_fallback = False
                self._record_provider_health("RECOVERED")
                for r in refs:
                    self.store.append(r.paper_position_id, {
                        "event": "RECOVERED", "ts": _now_utc_iso(), "provider": self.provider_name,
                        "note": "real-time feed recovered; resuming — gap NOT backfilled",
                    })
            return quotes, False
        except Exception as e:
            # real-time total loss
            self.state.last_error = str(e)
            if not (self.allow_fallback and self.fallback):
                self._record_provider_health("DISCONNECTED", str(e))
                for r in refs:
                    self.store.append(r.paper_position_id, {
                        "event": DISCONNECTED, "ts": _now_utc_iso(),
                        "provider": self.provider_name, "error": str(e),
                        "note": "real-time feed lost; no fallback configured — no quote fabricated",
                    })
                return [], False
            try:
                if not self.fallback.connected:
                    self.fallback.connect()
                quotes = self.fallback.get_quotes(refs)
                if not self.on_fallback:
                    self.on_fallback = True
                    self._record_provider_health("FALLBACK", str(e))
                    for r in refs:
                        self.store.append(r.paper_position_id, {
                            "event": FALLBACK_TO_CBOE, "ts": _now_utc_iso(),
                            "from_provider": self.provider_name, "to_provider": self.fallback_name,
                            "error": str(e),
                            "note": "real-time lost; using the DELAYED fallback floor (labelled), "
                                    "real-time history preserved, timestamps not mixed",
                        })
                return quotes, True
            except Exception as e2:
                for r in refs:
                    self.store.append(r.paper_position_id, {
                        "event": DISCONNECTED, "ts": _now_utc_iso(),
                        "provider": self.provider_name, "fallback_error": str(e2),
                        "note": "both real-time and CBOE fallback failed — no quote fabricated",
                    })
                return [], False

    def poll_once(self) -> Dict[str, Any]:
        """One tracking cycle over all currently-open positions."""
        now = _now_utc_iso()
        # DORMANT GUARD: without the primary credential the worker NEVER requests live quotes,
        # never fabricates, never marks positions live, never evaluates real-time exits. It only
        # heartbeats a clearly-labelled NOT_CONFIGURED status; Phase 1 delayed tracking is untouched.
        if not self.is_configured:
            self.state.status = NOT_CONFIGURED
            self.state.open_positions = len(load_open_positions(self.watchlist_path))
            self._publish()
            return {"ts": now, "status": NOT_CONFIGURED, "open": self.state.open_positions,
                    "receiving_realtime": 0, "note": "dormant — no primary credential installed"}
        cards = load_open_positions(self.watchlist_path)
        # drop positions this tracker has already simulated-closed (append-only; never reopen)
        cards = [c for c in cards
                 if not self.store.is_closed((c.get("paper_position") or {}).get("id") or c.get("contract_id"))]
        refs = refs_from_positions(cards)
        summary = {"ts": now, "open": len(refs), "receiving_realtime": 0,
                   "delayed_or_fallback": 0, "stale_or_disconnected": 0, "closed_this_cycle": 0}
        if not refs:
            return summary

        quotes, used_fallback = self._acquire_quotes(refs)
        qmap = {q.contract_id: q for q in quotes}
        card_by_cid = {c.get("contract_id"): c for c in cards}

        for r in refs:
            q = qmap.get(r.contract_id)
            health = feed_health(q, now)
            if health == LIVE:
                summary["receiving_realtime"] += 1
            elif health == DELAYED:
                summary["delayed_or_fallback"] += 1
            else:
                summary["stale_or_disconnected"] += 1

            # 1) always preserve the RAW observation verbatim (even if unusable)
            raw = {"event": "OBSERVATION", "ts": now, "feed_health": health,
                   "quote": q.as_dict() if q else None,
                   "observed_lag_sec": _lag_seconds(q.provider_quote_ts, now) if q else None}
            self.store.append(r.paper_position_id, raw)

            if q and q.mode == MODE_REALTIME and q.ok:
                self.last_realtime_update = now
                self.state.last_realtime_quote_ts = q.provider_quote_ts or now
                self.state.ever_received_realtime = True   # gates the dashboard LIVE badge
                self.state.provider_mode = MODE_REALTIME
            elif q and q.ok:
                self.last_delayed_update = now
                self.state.last_delayed_quote_ts = q.provider_quote_ts or now
                self.state.provider_mode = q.mode   # DELAYED or DELAYED_FALLBACK

            # 2) NEVER evaluate stops on a stale/disconnected/unusable feed
            if q is None or not q.ok or health in (STALE, DISCONNECTED):
                continue

            # 3) advance the FROZEN engine simulation with this real-time observation
            mirror = self._mirror(card_by_cid[r.contract_id])
            reason = self.engine._paper_step(mirror, q.to_res(), now)
            if reason:
                # exit condition FIRST OBSERVED — conservative exit at observed bid (mirror.exit_price)
                summary["closed_this_cycle"] += 1
                self.store.append(r.paper_position_id, {
                    "event": EXIT_FIRST_OBSERVED, "ts": now, "reason": reason,
                    "stop_level_at_observation": mirror.get("current_stop_level"),
                    "observed_bid": mirror.get("current_bid"),
                    "provider": q.provider, "provider_mode": q.mode,
                    "provider_quote_ts": q.provider_quote_ts,
                    "note": "condition first observed; NOT reconstructed at the stop price",
                })
                self.store.append(r.paper_position_id, {
                    "event": SIMULATED_CLOSED, "ts": now, "reason": reason,
                    "exit_price_observed_bid": mirror.get("exit_price"),
                    "final_return_pct": mirror.get("current_pct"),
                    "note": "research simulation — NOT a real fill, not a recommendation",
                })
                # descriptive analytics for the completed paper trade (measured facts only)
                self.store.append(r.paper_position_id,
                                  build_trade_analytics(mirror, card_by_cid[r.contract_id], reason))
        self.store.append("_heartbeat", self.heartbeat())
        # roll cycle results into the published worker state
        self.state.status = DEGRADED if self.on_fallback else RUNNING
        self.state.on_fallback = self.on_fallback
        self.state.open_positions = summary["open"]
        self.state.receiving_realtime = summary["receiving_realtime"]
        self.state.on_delayed_fallback = summary["delayed_or_fallback"]
        self.state.stale = summary["stale_or_disconnected"] if not self.provider.connected else 0
        self.state.disconnected = 0 if self.provider.connected else summary["stale_or_disconnected"]
        self._publish()
        self._checkpoint()
        return summary

    def run(self, interval_sec: int = 30, max_cycles: Optional[int] = None):
        """Persistent loop. NOT for GitHub Actions (never hold this open in Actions)."""
        cycles = 0
        while True:
            self.poll_once()
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return
            time.sleep(interval_sec)


# ------------------------------------------------------------------ health endpoint (Railway)
def make_health_server(host: str, port: int, tracker: "Tracker") -> ThreadingHTTPServer:
    """/health for Railway liveness (200 while the process is alive — a dormant NOT_CONFIGURED
    worker is healthy), /status for the full published snapshot."""
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # silence default stderr logging; we emit structured logs instead

        def _send(self, code, body):
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            snap = tracker.state.snapshot()
            if self.path.startswith("/status"):
                self._send(200, snap)
            elif self.path.startswith("/health"):
                self._send(200, {"status": "ok", "worker_status": snap["worker_status"],
                                 "heartbeat_ts": snap["heartbeat_ts"], "badge": snap["badge"]})
            else:
                self._send(200, {"service": "tracker-service", "schema_version": SCHEMA_VERSION,
                                 "worker_status": snap["worker_status"]})

    return ThreadingHTTPServer((host, port), _Handler)


def serve(tracker: "Tracker", host: str = "0.0.0.0", port: int = 8080, interval_sec: int = 20):
    """Railway entrypoint: run the health server + the poll loop until SIGTERM/SIGINT, then shut
    down gracefully (final checkpoint + published state). The loop keeps running through market
    hours; it never sleeps the process."""
    stop = threading.Event()

    def _on_signal(signum, _frame):
        log("shutdown_signal", signal=int(signum))
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    httpd = make_health_server(host, port, tracker)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log("worker_started", provider=tracker.provider_name, fallback=tracker.fallback_name,
        configured=tracker.is_configured, health_port=port, state_dir=tracker.state_dir)

    try:
        while not stop.is_set():
            try:
                s = tracker.poll_once()
                log("cycle", **{k: s.get(k) for k in ("status", "open", "receiving_realtime",
                                                       "closed_this_cycle") if k in s})
            except Exception as e:
                tracker.state.last_error = str(e)
                log("poll_error", error=str(e))
            stop.wait(interval_sec)
    finally:
        tracker.state.status = SHUTTING_DOWN
        tracker._checkpoint()
        tracker._publish()
        try:
            httpd.shutdown()
        except Exception:
            pass
        log("worker_stopped")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Real-time active-position tracker (Phase 2 scaffold)")
    ap.add_argument("--watchlist", required=True, help="path to Phase 1 research_watchlist.json (read-only)")
    ap.add_argument("--store", default="tracker-data", help="append-only event-store directory")
    ap.add_argument("--provider", default=None,
                    help=f"override the configured provider by name ({', '.join(available_providers())})")
    ap.add_argument("--fallback", default=None, help="override the configured fallback provider")
    ap.add_argument("--config", default=None, help="path to tracker.config.json")
    ap.add_argument("--engine", default=None, help="explicit path to research_scanner.py")
    ap.add_argument("--interval", type=int, default=None, help="poll interval seconds (else config)")
    ap.add_argument("--once", action="store_true", help="single cycle then exit")
    ap.add_argument("--serve", action="store_true", help="Railway mode: health endpoint + loop until SIGTERM")
    ap.add_argument("--no-fallback", action="store_true")
    args = ap.parse_args(argv)

    # provider selection lives ENTIRELY in config (CLI > env > file > default). The worker below
    # is handed instances and never names a broker.
    cfg = trackercfg.load_config(cli_provider=args.provider, cli_fallback=args.fallback,
                                 cli_allow_fallback=(False if args.no_fallback else None),
                                 config_path=args.config)
    engine = load_engine(args.engine)
    # in serve mode the event store + published state live on the persistent volume, never only
    # the ephemeral container filesystem.
    state_dir = cfg["state_dir"] if args.serve else None
    store_root = os.path.join(cfg["state_dir"], "events") if args.serve else args.store
    store = EventStore(store_root)
    provider = create_provider(cfg["provider"], **cfg["provider_kwargs"])
    fallback = (create_provider(cfg["fallback"], **cfg["fallback_kwargs"])
                if (cfg["allow_fallback"] and cfg["fallback"]) else None)
    tracker = Tracker(engine, args.watchlist, store, provider=provider, fallback=fallback,
                      allow_fallback=cfg["allow_fallback"], state_dir=state_dir)
    interval = args.interval or cfg["poll_interval_sec"]
    banner = (f"[tracker] {SCHEMA_VERSION} provider={cfg['provider']} fallback={cfg['fallback']} "
              f"configured={tracker.is_configured} status={tracker.state.status}")
    if args.serve:
        log("boot", version=SCHEMA_VERSION, provider=cfg["provider"], fallback=cfg["fallback"],
            configured=tracker.is_configured, status=tracker.state.status, state_dir=cfg["state_dir"])
        serve(tracker, port=cfg["health_port"], interval_sec=interval)
    elif args.once:
        print(banner)
        print(json.dumps(tracker.poll_once(), indent=2))
    else:
        print(banner)
        tracker.run(interval_sec=interval)


if __name__ == "__main__":
    main()
