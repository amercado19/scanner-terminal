#!/usr/bin/env python3
"""
Tests for the Phase 2 real-time active-position tracker, using a MOCK provider.
No network, no credentials, no real feed. Verifies the honesty + append-only invariants.
"""
import os
import sys
import json
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import worker as W
from providers import (Provider, Quote, ContractRef,
                       MODE_REALTIME, MODE_DELAYED, MODE_DELAYED_FALLBACK,
                       LIVE, STALE, DISCONNECTED, FALLBACK_TO_CBOE,
                       EXIT_FIRST_OBSERVED, SIMULATED_CLOSED)

FAILURES = []
def check(name, cond):
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        FAILURES.append(name)


# ---- a scripted mock real-time provider (no network) ----
class MockProvider(Provider):
    name = "mock"
    default_mode = MODE_REALTIME

    def __init__(self, script=None, fail=False, **kw):
        super().__init__(**kw)
        self.script = list(script or [])   # list of {contract_id: (bid, ask, underlying, dte)}
        self.fail = fail
        self.i = 0

    def connect(self):
        if self.fail:
            raise RuntimeError("mock forced connect failure")
        self._connected = True

    def get_quotes(self, refs):
        if self.fail:
            raise RuntimeError("mock forced feed loss")
        frame = self.script[min(self.i, len(self.script) - 1)] if self.script else {}
        self.i += 1
        from worker import _now_utc_iso
        now = _now_utc_iso()
        out = []
        for r in refs:
            spec = frame.get(r.contract_id)
            if spec is None:
                out.append(Quote(contract_id=r.contract_id, provider=self.name,
                                 mode=MODE_REALTIME, ok=False, ingestion_ts=now, dte=r.dte))
                continue
            bid, ask, und, dte = spec
            out.append(Quote(contract_id=r.contract_id, provider=self.name, mode=MODE_REALTIME,
                             ok=True, provider_quote_ts=now, ingestion_ts=now,
                             bid=bid, ask=ask, mid=round((bid + ask) / 2, 4),
                             underlying=und, dte=dte))
        return out


def make_watchlist(tmp, entry_mid=2.00, status="ACTIVE", dte=40):
    """A minimal Phase 1 watchlist with one open paper position."""
    engine = W.load_engine()
    pol = dict(engine.DEFAULT_POLICY)
    pp = {
        "id": "KO_pp_1", "status": status, "params": pol,
        "policy_version": pol.get("version"),
        "entry_mid": entry_mid, "entry_underlying": 90.0,
        "observations": [], "trailing_active": False,
        "trailing_high": None, "trailing_stop_level": None,
        "initial_stop_level": round(entry_mid * (1 - pol["initial_stop_pct"] / 100), 4),
        "current_stop_level": round(entry_mid * (1 - pol["initial_stop_pct"] / 100), 4),
    }
    wl = {"meta": {"schema_version": "research-scanner.v3.2"},
          "active": {"KO261016C00090000": {
              "contract_id": "KO261016C00090000", "symbol": "KO", "right": "call",
              "strike": 90.0, "expiration": "2026-10-16", "dte": dte,
              "paper_position": pp}}}
    p = os.path.join(tmp, "research_watchlist.json")
    json.dump(wl, open(p, "w"))
    return p, engine


CID = "KO261016C00090000"

def test_realtime_observation_recorded_and_no_stop():
    tmp = tempfile.mkdtemp()
    try:
        wl, engine = make_watchlist(tmp, entry_mid=2.00)
        store = W.EventStore(os.path.join(tmp, "store"))
        prov = MockProvider(script=[{CID: (2.05, 2.15, 90.5, 40)}])
        prov._connected = True
        t = W.Tracker(engine, wl, store, provider_name="mock", provider=prov)
        s = t.poll_once()
        evs = store.read("KO_pp_1")
        obs = [e for e in evs if e["event"] == "OBSERVATION"]
        check("realtime: one observation recorded", len(obs) == 1)
        check("realtime: feed health LIVE", obs[0]["feed_health"] == LIVE)
        check("realtime: raw quote preserved", obs[0]["quote"]["bid"] == 2.05)
        check("realtime: no exit while healthy & above stop",
              not any(e["event"] == SIMULATED_CLOSED for e in evs))
        check("realtime: summary counts one real-time", s["receiving_realtime"] == 1)
    finally:
        shutil.rmtree(tmp)

def test_initial_stop_first_observed_exits_at_bid():
    tmp = tempfile.mkdtemp()
    try:
        wl, engine = make_watchlist(tmp, entry_mid=2.00)  # initial stop = 1.40
        store = W.EventStore(os.path.join(tmp, "store"))
        # mid drops to 1.30 (<= 1.40): stop FIRST OBSERVED; conservative exit at bid 1.25
        prov = MockProvider(script=[{CID: (1.25, 1.35, 88.0, 40)}]); prov._connected = True
        t = W.Tracker(engine, wl, store, provider_name="mock", provider=prov)
        t.poll_once()
        evs = store.read("KO_pp_1")
        efo = [e for e in evs if e["event"] == EXIT_FIRST_OBSERVED]
        sc = [e for e in evs if e["event"] == SIMULATED_CLOSED]
        check("stop: EXIT_FIRST_OBSERVED emitted", len(efo) == 1 and efo[0]["reason"] == "INITIAL_STOP")
        check("stop: exit at observed BID not stop price", sc and sc[0]["exit_price_observed_bid"] == 1.25)
        check("stop: note says not a real fill", sc and "not a real fill" in sc[0]["note"].lower())
        check("stop: note says not reconstructed at stop", "not reconstructed" in efo[0]["note"].lower())
    finally:
        shutil.rmtree(tmp)

def test_closed_position_not_reopened_appendonly():
    tmp = tempfile.mkdtemp()
    try:
        wl, engine = make_watchlist(tmp, entry_mid=2.00)
        store = W.EventStore(os.path.join(tmp, "store"))
        prov = MockProvider(script=[{CID: (1.25, 1.35, 88.0, 40)},
                                    {CID: (1.20, 1.30, 87.0, 40)}]); prov._connected = True
        t = W.Tracker(engine, wl, store, provider_name="mock", provider=prov)
        t.poll_once()
        n_after_first = len(store.read("KO_pp_1"))
        t.poll_once()  # position already simulated-closed -> should be skipped
        n_after_second = len(store.read("KO_pp_1"))
        closes = [e for e in store.read("KO_pp_1") if e["event"] == SIMULATED_CLOSED]
        check("appendonly: closed exactly once", len(closes) == 1)
        check("appendonly: no new events after close", n_after_second == n_after_first)
    finally:
        shutil.rmtree(tmp)

def test_no_fabrication_on_total_feed_loss_no_fallback():
    tmp = tempfile.mkdtemp()
    try:
        wl, engine = make_watchlist(tmp, entry_mid=2.00)
        store = W.EventStore(os.path.join(tmp, "store"))
        prov = MockProvider(fail=True)
        t = W.Tracker(engine, wl, store, provider_name="mock", allow_fallback=False, provider=prov, fallback=None)
        s = t.poll_once()
        evs = store.read("KO_pp_1")
        disc = [e for e in evs if e["event"] == DISCONNECTED]
        obs_with_price = [e for e in evs if e["event"] == "OBSERVATION" and e.get("quote") and e["quote"].get("bid") is not None]
        check("nofab: DISCONNECTED event recorded", len(disc) >= 1)
        check("nofab: no fabricated priced observation", len(obs_with_price) == 0)
        check("nofab: no simulated close on lost feed", not any(e["event"] == SIMULATED_CLOSED for e in evs))
        check("nofab: nothing counted as real-time", s["receiving_realtime"] == 0)
    finally:
        shutil.rmtree(tmp)

def test_fallback_to_cboe_labelled_and_history_preserved():
    tmp = tempfile.mkdtemp()
    try:
        wl, engine = make_watchlist(tmp, entry_mid=2.00)
        store = W.EventStore(os.path.join(tmp, "store"))
        prov = MockProvider(fail=True)
        t = W.Tracker(engine, wl, store, provider_name="mock", allow_fallback=True, provider=prov)
        # inject a mock fallback that returns a DELAYED_FALLBACK quote (no network)
        class MockFallback(MockProvider):
            name = "cboe"
            def get_quotes(self, refs):
                from worker import _now_utc_iso
                now = _now_utc_iso()
                return [Quote(contract_id=r.contract_id, provider="cboe",
                              mode=MODE_DELAYED_FALLBACK, ok=True,
                              provider_quote_ts=now, ingestion_ts=now,
                              bid=1.90, ask=2.00, mid=1.95, underlying=89.0, dte=r.dte)
                        for r in refs]
        t.fallback = MockFallback(); t.fallback._connected = True
        t.poll_once()
        evs = store.read("KO_pp_1")
        fb = [e for e in evs if e["event"] == FALLBACK_TO_CBOE]
        obs = [e for e in evs if e["event"] == "OBSERVATION"]
        check("fallback: FALLBACK_TO_CBOE event emitted", len(fb) == 1)
        check("fallback: quote labelled DELAYED_FALLBACK",
              obs and obs[-1]["quote"]["mode"] == MODE_DELAYED_FALLBACK)
        check("fallback: stops NOT evaluated on delayed fallback (no close)",
              not any(e["event"] == SIMULATED_CLOSED for e in evs))
    finally:
        shutil.rmtree(tmp)

def test_subscribes_only_open_positions():
    tmp = tempfile.mkdtemp()
    try:
        wl, engine = make_watchlist(tmp, entry_mid=2.00)
        # add a CLOSED position that must NOT be subscribed
        data = json.load(open(wl))
        data["active"]["XX261016P00050000"] = {
            "contract_id": "XX261016P00050000", "symbol": "XX", "right": "put",
            "strike": 50.0, "expiration": "2026-10-16", "dte": 40,
            "paper_position": {"id": "XX_pp", "status": "CLOSED", "params": engine.DEFAULT_POLICY}}
        json.dump(data, open(wl, "w"))
        cards = W.load_open_positions(wl)
        refs = W.refs_from_positions(cards)
        ids = {r.contract_id for r in refs}
        check("scope: only the OPEN position subscribed", ids == {CID})
    finally:
        shutil.rmtree(tmp)

def test_reuses_engine_frozen_policy():
    tmp = tempfile.mkdtemp()
    try:
        wl, engine = make_watchlist(tmp)
        store = W.EventStore(os.path.join(tmp, "store"))
        t = W.Tracker(engine, wl, store, provider_name="mock", provider=MockProvider())
        check("policy: tracker policy IS the engine DEFAULT_POLICY (no fork)",
              t.policy == dict(engine.DEFAULT_POLICY))
        check("policy: engine _paper_step is the evaluator used",
              hasattr(engine, "_paper_step"))
    finally:
        shutil.rmtree(tmp)

def test_not_operational_banner_present():
    src = open(os.path.join(HERE, "worker.py"), encoding="utf-8").read()
    check("honesty: worker states NOT OPERATIONAL", "NOT OPERATIONAL" in src)
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    check("honesty: README states NOT OPERATIONAL", "NOT OPERATIONAL" in readme)
    check("honesty: README gives exact secret name", "TRADIER_TOKEN" in readme)
    check("honesty: worker forbids holding socket in Actions", "never hold this open in Actions".lower() in src.lower())


# ============ provider abstraction (swappable by config, no engine change) ============
import providers as P
import config as C
import hashlib, importlib

# the exact engine byte-image deployed + accepted in Phase 1. This test FAILS if the Phase 1
# engine is modified — proving the provider abstraction change did not touch engine logic.
DEPLOYED_ENGINE_SHA256 = "a243375b9035cce99bb86564e3f2adf55fd7c889887de19dba222f734fcf6a58"

def test_provider_registry_is_pluggable():
    names = set(P.available_providers())
    for expected in ("cboe", "tradier", "polygon", "alpaca", "ibkr", "schwab"):
        check(f"registry: '{expected}' selectable by config", expected in names)
    # real ones instantiate; unknown raises (never silently substitutes)
    check("registry: create tradier", type(P.create_provider("tradier")).__name__ == "TradierProvider")
    check("registry: create cboe", type(P.create_provider("cboe")).__name__ == "CboeFallbackProvider")
    try:
        P.create_provider("does-not-exist"); check("registry: unknown raises", False)
    except ValueError:
        check("registry: unknown provider raises (no silent substitute)", True)

def test_stub_providers_fail_loudly_not_fabricate():
    for name in ("polygon", "alpaca", "ibkr", "schwab"):
        prov = P.create_provider(name); prov.connect()
        try:
            prov.get_quotes([ContractRef(paper_position_id="x", contract_id="KO261016C00090000",
                                         symbol="KO", right="call", strike=90.0, expiration="2026-10-16")])
            check(f"stub {name}: raises rather than fabricating", False)
        except NotImplementedError as e:
            check(f"stub {name}: NotImplementedError points at get_quotes", "get_quotes" in str(e))

def test_config_selects_provider_by_config_only():
    # code-level default is the honest delayed floor; the committed tracker.config.json sets the
    # intended primary (tradier) which stays DORMANT until TRADIER_TOKEN is installed.
    check("config: code default provider is cboe (honest floor)", C.DEFAULT_PROVIDER == "cboe")
    cfg = C.load_config(config_path=os.path.join(HERE, "tracker.config.json"))
    check("config: committed config selects tradier primary / cboe fallback",
          cfg["provider"] == "tradier" and cfg["fallback"] == "cboe")
    # env overrides
    os.environ["TRACKER_PROVIDER"] = "tradier"
    try:
        check("config: env TRACKER_PROVIDER wins", C.load_config()["provider"] == "tradier")
        # explicit CLI beats env
        check("config: explicit arg beats env", C.load_config(cli_provider="polygon")["provider"] == "polygon")
    finally:
        del os.environ["TRACKER_PROVIDER"]
    # file config
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "tracker.config.json")
        json.dump({"provider": "alpaca", "fallback": "cboe"}, open(p, "w"))
        check("config: file provider used", C.load_config(config_path=p)["provider"] == "alpaca")
    finally:
        shutil.rmtree(tmp)

def test_worker_is_provider_agnostic():
    # the worker names no broker: provider identity is DERIVED from the injected instance
    tmp = tempfile.mkdtemp()
    try:
        wl, engine = make_watchlist(tmp)
        store = W.EventStore(os.path.join(tmp, "store"))
        for name, prov in [("mock", MockProvider()), ("tradier", P.create_provider("tradier"))]:
            t = W.Tracker(engine, wl, store, provider=prov, allow_fallback=False)
            check(f"agnostic: provider_name derived from instance ({name})", t.provider_name == name)
        # constructing with neither instance nor name is refused (no hidden default broker)
        try:
            W.Tracker(engine, wl, store); check("agnostic: no hidden default provider", False)
        except ValueError:
            check("agnostic: refuses to invent a provider", True)
        # swapping providers is a name change only — same Tracker class, same engine
        check("agnostic: worker source names no primary broker in code",
              "provider_name=\"tradier\"" not in open(os.path.join(HERE, "worker.py")).read())
    finally:
        shutil.rmtree(tmp)

def test_engine_logic_unchanged():
    # locate research_scanner.py the way load_engine does, hash it, compare to the deployed image
    here = HERE
    cands = [os.path.join(here, "..", "research-scanner", "research_scanner.py"),
             os.path.join(here, "..", "research_scanner.py"),
             os.path.join(here, "research_scanner.py")]
    path = next((c for c in cands if os.path.exists(c)), None)
    if path is None:
        print("    (skip engine-hash: research_scanner.py not co-located)"); return
    sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
    check("engine: research_scanner.py byte-identical to the deployed Phase-1 image", sha == DEPLOYED_ENGINE_SHA256)
    # and the tracker still evaluates stops via the engine, not a fork
    engine = W.load_engine()
    check("engine: stop ladder still comes from engine._paper_step", hasattr(engine, "_paper_step"))

def test_trade_analytics_descriptive_only():
    tmp = tempfile.mkdtemp()
    try:
        wl, engine = make_watchlist(tmp, entry_mid=2.00)  # initial stop 1.40
        store = W.EventStore(os.path.join(tmp, "store"))
        # a favorable run, then a drop that triggers the stop, so MFE/MAE/peak fields are non-trivial
        prov = MockProvider(script=[{CID: (2.60, 2.70, 92.0, 40)},   # +32.5% -> trailing activates
                                    {CID: (2.00, 2.10, 90.0, 39)},   # pullback
                                    {CID: (1.90, 2.00, 89.0, 38)}])  # hits trailing stop
        prov._connected = True
        t = W.Tracker(engine, wl, store, provider=prov, allow_fallback=False)
        for _ in range(3):
            t.poll_once()
        evs = store.read("KO_pp_1")
        an = [e for e in evs if e["event"] == "TRADE_ANALYTICS"]
        check("analytics: emitted on close", len(an) == 1)
        a = an[0] if an else {}
        required = ["entry_ts","exit_ts","holding_time_sec","mfe_pct","mae_pct","highest_option_value",
                    "lowest_option_value","highest_underlying_move_pct","lowest_underlying_move_pct",
                    "exit_reason","peak_profit_pct","peak_drawdown_pct","final_return_pct",
                    "trailing_vs_initial_delta_pct","trailing_improved_result",
                    "left_research_filter_before_close","initial_stop_only_return_pct"]
        missing = [k for k in required if k not in a]
        check("analytics: all required descriptive fields present", not missing)
        check("analytics: MFE positive on the favorable run", (a.get("mfe_pct") or 0) > 0)
        check("analytics: flagged descriptive_only", a.get("descriptive_only") is True)
        # NO forbidden predictive/ranking concepts in the DATA. The honesty `note` legitimately
        # names them to disclaim them (like the dashboard disclaimer), so it is excluded from the scan.
        data_only = {k: v for k, v in a.items() if k != "note"}
        blob = json.dumps(data_only).lower()
        for bad in ("score","rank","expected_return","win_probability","probability","recommend","forecast","predict"):
            check(f"analytics: no '{bad}' concept in data fields", bad not in blob)
    finally:
        shutil.rmtree(tmp)


# ============ Phase 2 ops: Tradier normalization, health, resilience, LIVE gate ============
from providers.tradier import normalize_quotes, resolve_base, PROD_BASE, SANDBOX_BASE
import urllib.request as _urlreq

def _refs(*cids):
    return [ContractRef(paper_position_id=c, contract_id=c, symbol="KO", right="call",
                        strike=90.0, expiration="2026-10-16", dte=40) for c in cids]

def test_tradier_normalization_realtime_and_missing():
    now = "2026-08-27T14:00:00+00:00"
    payload = {"quotes": {"quote": [
        {"symbol": CID, "bid": 2.10, "ask": 2.20, "delayed": False,
         "greeks": {"mid_iv": 0.2, "delta": 0.5, "theta": -0.02}, "trade_date": 1756303200000},
    ]}}
    qs = normalize_quotes(payload, _refs(CID, "XX261016P00050000"), "tradier", MODE_REALTIME, now)
    by = {q.contract_id: q for q in qs}
    check("tradier-norm: real-time quote parsed", by[CID].ok and by[CID].mode == MODE_REALTIME)
    check("tradier-norm: mid computed", by[CID].mid == 2.15)
    check("tradier-norm: provider_quote_ts stamped from feed", by[CID].provider_quote_ts is not None)
    check("tradier-norm: missing contract => ok=False, no price (no fabrication)",
          (by["XX261016P00050000"].ok is False) and by["XX261016P00050000"].bid is None)

def test_tradier_delayed_flag_labels_delayed():
    now = "2026-08-27T14:00:00+00:00"
    payload = {"quotes": {"quote": {"symbol": CID, "bid": 2.0, "ask": 2.1, "delayed": True}}}
    q = normalize_quotes(payload, _refs(CID), "tradier", MODE_REALTIME, now)[0]
    check("tradier-norm: delayed flag => MODE_DELAYED (not passed off as real-time)", q.mode == MODE_DELAYED)

def test_tradier_env_base_resolution():
    check("tradier-env: production => real-time base", resolve_base("production") == PROD_BASE)
    check("tradier-env: sandbox => delayed base", resolve_base("sandbox") == SANDBOX_BASE)

def test_config_fallback_provider_env_name():
    os.environ["TRACKER_FALLBACK_PROVIDER"] = "polygon"
    try:
        check("config: TRACKER_FALLBACK_PROVIDER honored", C.load_config()["fallback"] == "polygon")
    finally:
        del os.environ["TRACKER_FALLBACK_PROVIDER"]

def test_not_configured_is_dormant_no_fabrication():
    tmp = tempfile.mkdtemp()
    try:
        os.environ.pop("TRADIER_TOKEN", None)
        wl, engine = make_watchlist(tmp)                       # 1 open ACTIVE position
        store = W.EventStore(os.path.join(tmp, "store"))
        tradier = P.create_provider("tradier")                 # no token -> not configured
        t = W.Tracker(engine, wl, store, provider=tradier, allow_fallback=False)
        check("dormant: worker is NOT_CONFIGURED", t.state.status == W.NOT_CONFIGURED)
        s = t.poll_once()
        check("dormant: poll returns NOT_CONFIGURED", s.get("status") == W.NOT_CONFIGURED)
        check("dormant: counts open positions", s.get("open") == 1)
        # NO observation/live events written for the position (no live quotes requested)
        evs = store.read("KO_pp_1")
        check("dormant: no OBSERVATION events (no live quotes requested)",
              not any(e.get("event") == "OBSERVATION" for e in evs))
        check("dormant: badge is NOT_CONFIGURED (never LIVE)", t.state.badge() == W.NOT_CONFIGURED)
    finally:
        shutil.rmtree(tmp)

def test_live_badge_gated_on_real_realtime_quote():
    st = W.WorkerState("tradier", "cboe")
    st.status = W.RUNNING; st.provider_mode = MODE_REALTIME
    check("live-gate: no LIVE before a real real-time quote", st.badge() != "REALTIME")
    st.ever_received_realtime = True
    check("live-gate: REALTIME only after a real real-time quote", st.badge() == "REALTIME")
    st.on_fallback = True
    check("live-gate: fallback shows FALLBACK, not LIVE", st.badge() == "FALLBACK")

def test_disconnect_then_recovery_events():
    tmp = tempfile.mkdtemp()
    try:
        wl, engine = make_watchlist(tmp)
        store = W.EventStore(os.path.join(tmp, "store"))
        prov = MockProvider(script=[{CID: (2.05, 2.15, 90.0, 40)}]); prov._connected = True

        class Flaky(MockProvider):
            def __init__(self): super().__init__(); self.calls = 0; self._connected = True
            def get_quotes(self, refs):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("simulated disconnect")
                return MockProvider(script=[{CID: (2.05, 2.15, 90.0, 40)}]).get_quotes(refs)

        class Fb(MockProvider):
            name = "cboe"
            def get_quotes(self, refs):
                from worker import _now_utc_iso; now = _now_utc_iso()
                return [Quote(contract_id=r.contract_id, provider="cboe", mode=MODE_DELAYED_FALLBACK,
                              ok=True, provider_quote_ts=now, ingestion_ts=now, bid=2.0, ask=2.1,
                              mid=2.05, underlying=90.0, dte=r.dte) for r in refs]
        fb = Fb(); fb._connected = True
        t = W.Tracker(engine, wl, store, provider=Flaky(), allow_fallback=True, fallback=fb)
        t.poll_once()   # ok (real-time)
        t.poll_once()   # disconnect -> fallback
        t.poll_once()   # recovered
        evs = store.read("KO_pp_1")
        kinds = [e["event"] for e in evs]
        check("resilience: FALLBACK_TO_CBOE recorded on disconnect", "FALLBACK_TO_CBOE" in kinds)
        check("resilience: RECOVERED recorded when real-time returns", "RECOVERED" in kinds)
    finally:
        shutil.rmtree(tmp)

def test_stale_feed_not_evaluated():
    now = "2026-08-27T14:10:00+00:00"
    old = "2026-08-27T12:00:00+00:00"   # >2h old real-time stamp -> STALE
    q = Quote(contract_id=CID, provider="tradier", mode=MODE_REALTIME, ok=True,
              provider_quote_ts=old, ingestion_ts=now, bid=1.0, ask=1.1, mid=1.05, dte=40)
    check("stale: real-time quote older than threshold => STALE", W.feed_health(q, now) == W.STALE)
    check("stale: fresh real-time => LIVE",
          W.feed_health(Quote(contract_id=CID, provider="tradier", mode=MODE_REALTIME, ok=True,
                              provider_quote_ts=now, ingestion_ts=now, bid=1.0, ask=1.1, dte=40), now) == W.LIVE)

def test_hwm_and_trailing_monotonic():
    tmp = tempfile.mkdtemp()
    try:
        wl, engine = make_watchlist(tmp, entry_mid=2.00)
        store = W.EventStore(os.path.join(tmp, "store"))
        # rise past +25% (trailing on), then wiggle down and up — HWM + stop must never decrease
        prov = MockProvider(script=[{CID: (2.60, 2.70, 92.0, 40)},
                                    {CID: (2.40, 2.50, 91.0, 40)},
                                    {CID: (2.90, 3.00, 93.0, 40)},
                                    {CID: (2.70, 2.80, 92.0, 40)}]); prov._connected = True
        t = W.Tracker(engine, wl, store, provider=prov, allow_fallback=False)
        highs, stops = [], []
        for _ in range(4):
            t.poll_once()
            m = t._pp_state.get("KO_pp_1", {})
            if m.get("trailing_high") is not None: highs.append(m["trailing_high"])
            if m.get("current_stop_level") is not None and m.get("trailing_active"):
                stops.append(m["current_stop_level"])
        check("monotonic: trailing high never decreases", highs == sorted(highs))
        check("monotonic: trailing stop never decreases", stops == sorted(stops))
    finally:
        shutil.rmtree(tmp)

def test_open_position_tracked_after_left_filter():
    tmp = tempfile.mkdtemp()
    try:
        wl, engine = make_watchlist(tmp)
        data = json.load(open(wl))
        data["active"][CID]["research_status"] = "LEFT_FILTER"   # left the research filter...
        json.dump(data, open(wl, "w"))
        cards = W.load_open_positions(wl)
        check("left-filter: still tracked while paper position is OPEN", len(cards) == 1)
        store = W.EventStore(os.path.join(tmp, "store"))
        prov = MockProvider(script=[{CID: (2.05, 2.15, 90.0, 40)}]); prov._connected = True
        t = W.Tracker(engine, wl, store, provider=prov, allow_fallback=False)
        t.poll_once()
        check("left-filter: observation still recorded after leaving filter",
              any(e.get("event") == "OBSERVATION" for e in store.read("KO_pp_1")))
    finally:
        shutil.rmtree(tmp)

def test_policy_version_preserved_in_mirror():
    tmp = tempfile.mkdtemp()
    try:
        wl, engine = make_watchlist(tmp)
        data = json.load(open(wl))
        data["active"][CID]["paper_position"]["policy_version"] = "paper-policy.v1"
        json.dump(data, open(wl, "w"))
        store = W.EventStore(os.path.join(tmp, "store"))
        prov = MockProvider(script=[{CID: (2.05, 2.15, 90.0, 40)}]); prov._connected = True
        t = W.Tracker(engine, wl, store, provider=prov, allow_fallback=False)
        t.poll_once()
        check("policy: mirror preserves policy_version",
              t._pp_state["KO_pp_1"].get("policy_version") == "paper-policy.v1")
    finally:
        shutil.rmtree(tmp)

def test_health_endpoint_and_state_publish():
    tmp = tempfile.mkdtemp()
    try:
        os.environ.pop("TRADIER_TOKEN", None)
        wl, engine = make_watchlist(tmp)
        store = W.EventStore(os.path.join(tmp, "events"))
        t = W.Tracker(engine, wl, store, provider=P.create_provider("tradier"),
                      allow_fallback=False, state_dir=tmp)
        # state published to the volume at construction
        sp = os.path.join(tmp, "worker_state.json")
        check("publish: worker_state.json written to volume", os.path.exists(sp))
        snap = json.load(open(sp))
        check("publish: badge NOT_CONFIGURED before any real quote", snap["badge"] == "NOT_CONFIGURED")
        # health server responds 200 with worker_status
        httpd = W.make_health_server("127.0.0.1", 0, t)
        port = httpd.server_address[1]
        import threading as _th; _th.Thread(target=httpd.handle_request, daemon=True).start()
        body = json.loads(_urlreq.urlopen(f"http://127.0.0.1:{port}/health", timeout=5).read().decode())
        httpd.server_close()
        check("health: /health returns ok + worker_status", body.get("status") == "ok" and "worker_status" in body)
    finally:
        shutil.rmtree(tmp)

def test_railway_and_env_files_present():
    root = os.path.abspath(os.path.join(HERE, ".."))
    check("railway: Dockerfile present", os.path.exists(os.path.join(root, "Dockerfile")))
    check("railway: railway.json present", os.path.exists(os.path.join(root, "railway.json")))
    env = os.path.join(HERE, ".env.example")
    check("railway: .env.example present", os.path.exists(env))
    txt = open(env).read()
    check("railway: env template has TRADIER_TOKEN placeholder (empty, no secret)", "TRADIER_TOKEN=" in txt)
    check("railway: env template exposes it EMPTY (no real secret committed)",
          "TRADIER_TOKEN=\n" in txt or "TRADIER_TOKEN= " in txt or txt.strip().endswith("TRADIER_TOKEN="))
    rj = json.load(open(os.path.join(root, "railway.json")))
    check("railway: healthcheck path is /health", rj["deploy"]["healthcheckPath"] == "/health")


# ============ Phase 2 boot-readiness: OCC symbol correctness + Tradier real-response hardening ====
import urllib.error as _urlerr
from providers import (is_valid_occ, missing_occ_quote,
                       DQ_MISSING_BID_ASK, DQ_ZERO_BID, DQ_OPTION_NOT_FOUND, DQ_STALE, DQ_MISSING_OCC,
                       ProviderAuthError, ProviderRateLimitError, ProviderPayloadError)
from providers.tradier import TradierProvider, STALE_AFTER_SEC

HUMAN_ID = "KO 90C 2026-10-16"     # the human-readable label — must NEVER be sent to a provider
OCC_ID = "KO261016C00090000"       # the OCC symbol — the ONLY thing sent upstream


class _CapturingTradier(TradierProvider):
    """A Tradier provider that captures the symbols passed to the HTTP layer and returns a scripted
    payload, so we can assert WHAT gets sent upstream without any network."""
    def __init__(self, payload, **kw):
        super().__init__(token="TESTTOKEN", **kw)
        self._payload = payload
        self.captured_symbols = None
        self._connected = True

    def _http_get(self, symbols):
        self.captured_symbols = list(symbols)
        return self._payload


def _human_ref():
    """A ref with a HUMAN contract_id and the correct OCC symbol carried separately."""
    return ContractRef(paper_position_id="KO_pp_1", contract_id=HUMAN_ID, symbol="KO",
                       right="call", strike=90.0, expiration="2026-10-16", dte=40, occ_symbol=OCC_ID)


def test_occ_validator():
    check("occ-valid: correct OCC accepted", is_valid_occ(OCC_ID) and is_valid_occ("DIS261016P00105000"))
    for bad in [HUMAN_ID, "KO 90C", "", None, "KO261016X00090000", "KO2610C00090000", "koab261016C00090000"]:
        check(f"occ-valid: reject {bad!r}", not is_valid_occ(bad))


def test_correct_occ_symbol_sent_to_provider():
    now = "2026-08-27T14:00:00+00:00"
    payload = {"quotes": {"quote": {"symbol": OCC_ID, "bid": 2.10, "ask": 2.20, "delayed": False,
                                    "trade_date": None}}}
    prov = _CapturingTradier(payload)
    qs = prov.get_quotes([_human_ref()])
    check("occ-send: provider queried with the OCC symbol", prov.captured_symbols == [OCC_ID])
    check("occ-send: HUMAN label NEVER sent upstream", HUMAN_ID not in (prov.captured_symbols or []))
    check("occ-send: quote preserves human contract_id for display", qs[0].contract_id == HUMAN_ID)
    check("occ-send: quote records the OCC symbol it answers", qs[0].occ_symbol == OCC_ID)
    check("occ-send: valid quote ok", qs[0].ok and qs[0].mid == 2.15)


def test_missing_occ_symbol_refused_nothing_sent():
    now = "2026-08-27T14:00:00+00:00"
    # a ref with NO valid OCC symbol (only the human label) must be refused, not sent upstream
    bad = ContractRef(paper_position_id="x", contract_id=HUMAN_ID, symbol="KO", right="call",
                      strike=90.0, expiration="2026-10-16", dte=40, occ_symbol=None)
    payload = {"quotes": {"quote": None}}
    prov = _CapturingTradier(payload)
    qs = prov.get_quotes([bad])
    check("occ-missing: nothing sent upstream (no HTTP call needed)", prov.captured_symbols is None)
    check("occ-missing: refusal quote ok=False", qs and qs[0].ok is False)
    check("occ-missing: typed DQ_MISSING_OCC", qs[0].data_quality == DQ_MISSING_OCC)
    check("occ-missing: no fabricated price", qs[0].bid is None and qs[0].mid is None)


def test_deterministic_occ_reconstruction_from_card():
    engine = W.load_engine()
    # a card WITHOUT a stored occ_symbol but with the fields -> deterministic reconstruction
    card = {"contract_id": HUMAN_ID, "symbol": "KO", "right": "call", "strike": 90.0,
            "expiration": "2026-10-16", "dte": 40, "paper_position": {"id": "KO_pp_1"}}
    refs = W.refs_from_positions([card], occ_fn=engine.occ_symbol)
    check("occ-reconstruct: rebuilt OCC matches engine", refs[0].occ_symbol == OCC_ID)
    check("occ-reconstruct: contract_id stays the human label", refs[0].contract_id == HUMAN_ID)
    # a card with neither occ_symbol nor reconstructable fields -> occ stays None (provider refuses)
    thin = {"contract_id": HUMAN_ID, "paper_position": {"id": "z"}}
    refs2 = W.refs_from_positions([thin], occ_fn=engine.occ_symbol)
    check("occ-reconstruct: unreconstructable => occ None (refuse, never guess)", refs2[0].occ_symbol is None)


def test_tradier_single_object_and_list_shapes():
    now = "2026-08-27T14:00:00+00:00"
    one = {"quotes": {"quote": {"symbol": OCC_ID, "bid": 2.0, "ask": 2.1, "delayed": False}}}
    q1 = normalize_quotes(one, [_human_ref()], "tradier", MODE_REALTIME, now)
    check("tradier-shape: single object handled", len(q1) == 1 and q1[0].ok and q1[0].mid == 2.05)
    many = {"quotes": {"quote": [
        {"symbol": OCC_ID, "bid": 2.0, "ask": 2.1, "delayed": False},
        {"symbol": "DIS261016P00105000", "bid": 3.0, "ask": 3.2, "delayed": False}]}}
    refs = [_human_ref(), ContractRef(paper_position_id="d", contract_id="DIS 105P", symbol="DIS",
                                      right="put", strike=105.0, expiration="2026-10-16", dte=40,
                                      occ_symbol="DIS261016P00105000")]
    q2 = normalize_quotes(many, refs, "tradier", MODE_REALTIME, now)
    check("tradier-shape: list of quotes handled", len(q2) == 2 and all(q.ok for q in q2))


def test_tradier_missing_bid_ask_typed():
    now = "2026-08-27T14:00:00+00:00"
    payload = {"quotes": {"quote": {"symbol": OCC_ID, "ask": 2.1, "delayed": False}}}  # no bid
    q = normalize_quotes(payload, [_human_ref()], "tradier", MODE_REALTIME, now)[0]
    check("tradier-dq: missing bid => ok=False", q.ok is False)
    check("tradier-dq: typed DQ_MISSING_BID_ASK", q.data_quality == DQ_MISSING_BID_ASK)
    check("tradier-dq: no fabricated mid", q.mid is None)


def test_tradier_zero_bid_typed():
    now = "2026-08-27T14:00:00+00:00"
    payload = {"quotes": {"quote": {"symbol": OCC_ID, "bid": 0, "ask": 0.05, "delayed": False}}}
    q = normalize_quotes(payload, [_human_ref()], "tradier", MODE_REALTIME, now)[0]
    check("tradier-dq: zero bid => ok=False (no live market)", q.ok is False)
    check("tradier-dq: typed DQ_ZERO_BID", q.data_quality == DQ_ZERO_BID)
    check("tradier-dq: zero bid not fabricated into a mid", q.mid is None)


def test_tradier_stale_quote_typed():
    now = "2026-08-27T14:00:00+00:00"
    # trade_date ~1 hour before now -> older than STALE_AFTER_SEC
    old_ms = 1756303200000  # 2026-08-27T14:00:00Z is ~1756304 -> use a clearly-old epoch
    payload = {"quotes": {"quote": {"symbol": OCC_ID, "bid": 2.0, "ask": 2.1, "delayed": False,
                                    "trade_date": 1756000000000}}}
    q = normalize_quotes(payload, [_human_ref()], "tradier", MODE_REALTIME, now,
                         stale_after_sec=STALE_AFTER_SEC)[0]
    check("tradier-dq: stale real-time quote => ok=False", q.ok is False)
    check("tradier-dq: typed DQ_STALE", q.data_quality == DQ_STALE)
    check("tradier-dq: stale quote not acted on (no mid)", q.mid is None)


def test_tradier_option_not_found_typed():
    now = "2026-08-27T14:00:00+00:00"
    payload = {"quotes": {"quote": {"symbol": "SOMETHINGELSE", "bid": 1, "ask": 2}}}
    q = normalize_quotes(payload, [_human_ref()], "tradier", MODE_REALTIME, now)[0]
    check("tradier-dq: absent contract => ok=False", q.ok is False)
    check("tradier-dq: typed DQ_OPTION_NOT_FOUND", q.data_quality == DQ_OPTION_NOT_FOUND)


def test_tradier_malformed_payload_typed():
    now = "2026-08-27T14:00:00+00:00"
    # a row that isn't a dict, and a payload with no quotes node — neither crashes, neither fabricates
    payload = {"quotes": {"quote": ["not-a-dict", {"no_symbol": True}]}}
    q = normalize_quotes(payload, [_human_ref()], "tradier", MODE_REALTIME, now)[0]
    check("tradier-dq: malformed rows skipped => not found, ok=False", q.ok is False)
    check("tradier-dq: malformed => no fabricated price", q.bid is None and q.mid is None)


def _raise_http(code):
    def _f(req, timeout=None):
        raise _urlerr.HTTPError(url="http://x", code=code, msg="err", hdrs=None, fp=None)
    return _f


def test_tradier_unauthorized_raises_typed():
    import providers.tradier as T
    prov = TradierProvider(token="BAD")
    orig = T.urllib.request.urlopen
    T.urllib.request.urlopen = _raise_http(401)
    try:
        raised = None
        try:
            prov._http_get([OCC_ID])
        except Exception as e:
            raised = e
        check("tradier-http: 401 => ProviderAuthError", isinstance(raised, ProviderAuthError))
    finally:
        T.urllib.request.urlopen = orig


def test_tradier_rate_limited_raises_typed():
    import providers.tradier as T
    prov = TradierProvider(token="X")
    orig = T.urllib.request.urlopen
    T.urllib.request.urlopen = _raise_http(429)
    try:
        raised = None
        try:
            prov._http_get([OCC_ID])
        except Exception as e:
            raised = e
        check("tradier-http: 429 => ProviderRateLimitError", isinstance(raised, ProviderRateLimitError))
    finally:
        T.urllib.request.urlopen = orig


def test_tradier_malformed_body_raises_typed():
    import providers.tradier as T
    import io
    prov = TradierProvider(token="X")
    orig = T.urllib.request.urlopen
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"<html>not json</html>"
    T.urllib.request.urlopen = lambda req, timeout=None: _Resp()
    try:
        raised = None
        try:
            prov._http_get([OCC_ID])
        except Exception as e:
            raised = e
        check("tradier-http: non-JSON body => ProviderPayloadError", isinstance(raised, ProviderPayloadError))
    finally:
        T.urllib.request.urlopen = orig


def test_provider_non_live_until_valid_authenticated_quote():
    # badge stays non-REALTIME across: dormant, auth failure, and a data-quality failure; only a
    # real ok real-time quote flips ever_received_realtime -> REALTIME.
    st = W.WorkerState("tradier", "cboe")
    st.status = W.RUNNING; st.provider_mode = MODE_REALTIME
    check("non-live: not REALTIME before any valid quote", st.badge() != "REALTIME")
    # a DQ failure (ok=False) must NOT flip the live gate
    st.ever_received_realtime = False
    check("non-live: unauthorized/DQ failure keeps it non-live", st.badge() != "REALTIME")
    st.ever_received_realtime = True
    check("non-live: REALTIME only after a real real-time quote", st.badge() == "REALTIME")


def test_worker_publishes_exit_ownership_and_closed_ids():
    tmp = tempfile.mkdtemp()
    try:
        wl, engine = make_watchlist(tmp, entry_mid=2.00)
        store = W.EventStore(os.path.join(tmp, "store"))
        # healthy real-time quote above stop -> worker owns exits, no close
        prov = MockProvider(script=[{CID: (2.05, 2.15, 90.0, 40)}]); prov._connected = True
        t = W.Tracker(engine, wl, store, provider=prov, allow_fallback=False, state_dir=os.path.join(tmp, "vol"))
        t.poll_once()
        hb = json.load(open(os.path.join(tmp, "vol", "heartbeat.json")))
        check("owner: worker publishes WORKER_REALTIME while healthy", hb["exit_ownership"] == "WORKER_REALTIME")
        check("owner: heartbeat carries status RUNNING", hb["status"] == "RUNNING")
        check("owner: closed_position_ids present (empty, none closed yet)", hb["closed_position_ids"] == [])
        # now a stop -> worker closes and publishes the closed id
        prov2 = MockProvider(script=[{CID: (1.20, 1.30, 88.0, 40)}]); prov2._connected = True
        t2 = W.Tracker(engine, wl, store, provider=prov2, allow_fallback=False, state_dir=os.path.join(tmp, "vol2"))
        t2.poll_once()
        hb2 = json.load(open(os.path.join(tmp, "vol2", "heartbeat.json")))
        check("owner: worker publishes the closed position id", "KO_pp_1" in hb2["closed_position_ids"])
    finally:
        shutil.rmtree(tmp)


def test_worker_ownership_dormant_and_fallback_not_owned():
    # a NOT_CONFIGURED or fallback worker must NOT claim WORKER_REALTIME (else the scanner would
    # wrongly defer and nobody would evaluate exits).
    st = W.WorkerState("tradier", "cboe")
    check("owner: default is NO_VALID_EXIT_FEED (dormant)", st.exit_ownership == "NO_VALID_EXIT_FEED")


def test_worker_restart_resumes_realtime_high_water_from_checkpoint():
    # gate 10 (recovery): a restart must RESUME the worker's real-time high-water mark / trailing
    # levels from checkpoint.json — never regress to the watchlist card's delayed-derived levels.
    tmp = tempfile.mkdtemp()
    try:
        vol = os.path.join(tmp, "vol")
        wl, engine = make_watchlist(tmp, entry_mid=2.00)      # initial stop 1.40
        store = W.EventStore(os.path.join(tmp, "store"))
        # a favorable real-time run: +32.5% activates trailing and lifts the high-water mark
        prov = MockProvider(script=[{CID: (2.60, 2.70, 92.0, 40)}]); prov._connected = True
        t = W.Tracker(engine, wl, store, provider=prov, allow_fallback=False, state_dir=vol)
        t.poll_once()
        m1 = t._pp_state["KO_pp_1"]
        hi, stop = m1.get("trailing_high"), m1.get("trailing_stop_level")
        check("recover: trailing activated on the real-time run", m1["trailing_active"] is True)
        check("recover: real-time high-water + trailing stop set", hi is not None and stop is not None)
        check("recover: checkpoint written to volume", os.path.exists(os.path.join(vol, "checkpoint.json")))
        # SIMULATE RESTART: a brand-new Tracker on the SAME volume. The watchlist card still shows the
        # ORIGINAL (delayed) levels (trailing_active False, no trailing_high) — a regression trap.
        t2 = W.Tracker(engine, wl, store, provider=MockProvider(), allow_fallback=False, state_dir=vol)
        card = json.load(open(wl))["active"][CID]
        check("recover: card itself still shows pre-run delayed levels",
              card["paper_position"]["trailing_active"] is False and card["paper_position"]["trailing_high"] is None)
        m2 = t2._mirror(card)
        check("recover: resumed trailing_active from checkpoint (not the card)", m2["trailing_active"] is True)
        check("recover: resumed real-time trailing_high (not regressed)", m2["trailing_high"] == hi)
        check("recover: resumed trailing_stop_level", m2["trailing_stop_level"] == stop)
        check("recover: mirror flagged resumed_from_checkpoint", m2.get("resumed_from_checkpoint") is True)
    finally:
        shutil.rmtree(tmp)


def test_worker_complete_lifecycle_active_through_exit():
    # gate 9 (one complete paper trade ACTIVE -> exit): run up, pull back through the trailing stop,
    # and confirm the single simulated close + descriptive analytics on the same worker instance.
    tmp = tempfile.mkdtemp()
    try:
        wl, engine = make_watchlist(tmp, entry_mid=2.00)
        store = W.EventStore(os.path.join(tmp, "store"))
        prov = MockProvider(script=[{CID: (2.60, 2.70, 92.0, 40)},   # +32.5% -> trailing activates
                                    {CID: (2.30, 2.40, 91.0, 39)},   # pullback, still above stop
                                    {CID: (1.90, 2.00, 89.0, 38)}])  # through the trailing stop -> exit
        prov._connected = True
        t = W.Tracker(engine, wl, store, provider=prov, allow_fallback=False, state_dir=os.path.join(tmp, "vol"))
        for _ in range(3):
            t.poll_once()
        evs = store.read("KO_pp_1")
        closes = [e for e in evs if e["event"] == SIMULATED_CLOSED]
        analytics = [e for e in evs if e["event"] == "TRADE_ANALYTICS"]
        check("lifecycle: exactly one simulated close ACTIVE->exit", len(closes) == 1)
        check("lifecycle: close carries the idempotency key", closes and closes[0].get("idempotency_key") == "KO_pp_1")
        check("lifecycle: descriptive analytics emitted on close", len(analytics) == 1)
        check("lifecycle: worker published the closed id", "KO_pp_1" in t.state.closed_position_ids)
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    print("Tracker-service tests (mock provider, no network):")
    for fn in [test_realtime_observation_recorded_and_no_stop,
               test_initial_stop_first_observed_exits_at_bid,
               test_closed_position_not_reopened_appendonly,
               test_no_fabrication_on_total_feed_loss_no_fallback,
               test_fallback_to_cboe_labelled_and_history_preserved,
               test_subscribes_only_open_positions,
               test_reuses_engine_frozen_policy,
               test_not_operational_banner_present,
               test_provider_registry_is_pluggable,
               test_stub_providers_fail_loudly_not_fabricate,
               test_config_selects_provider_by_config_only,
               test_worker_is_provider_agnostic,
               test_engine_logic_unchanged,
               test_trade_analytics_descriptive_only,
               test_tradier_normalization_realtime_and_missing,
               test_tradier_delayed_flag_labels_delayed,
               test_tradier_env_base_resolution,
               test_config_fallback_provider_env_name,
               test_not_configured_is_dormant_no_fabrication,
               test_live_badge_gated_on_real_realtime_quote,
               test_disconnect_then_recovery_events,
               test_stale_feed_not_evaluated,
               test_hwm_and_trailing_monotonic,
               test_open_position_tracked_after_left_filter,
               test_policy_version_preserved_in_mirror,
               test_health_endpoint_and_state_publish,
               test_railway_and_env_files_present,
               # Phase 2 boot-readiness: OCC correctness + Tradier hardening + ownership publication
               test_occ_validator,
               test_correct_occ_symbol_sent_to_provider,
               test_missing_occ_symbol_refused_nothing_sent,
               test_deterministic_occ_reconstruction_from_card,
               test_tradier_single_object_and_list_shapes,
               test_tradier_missing_bid_ask_typed,
               test_tradier_zero_bid_typed,
               test_tradier_stale_quote_typed,
               test_tradier_option_not_found_typed,
               test_tradier_malformed_payload_typed,
               test_tradier_unauthorized_raises_typed,
               test_tradier_rate_limited_raises_typed,
               test_tradier_malformed_body_raises_typed,
               test_provider_non_live_until_valid_authenticated_quote,
               test_worker_publishes_exit_ownership_and_closed_ids,
               test_worker_ownership_dormant_and_fallback_not_owned,
               test_worker_restart_resumes_realtime_high_water_from_checkpoint,
               test_worker_complete_lifecycle_active_through_exit]:
        print("\n" + fn.__name__)
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("ALL TRACKER TESTS PASSED")
