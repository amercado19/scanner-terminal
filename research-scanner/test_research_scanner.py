#!/usr/bin/env python3
"""Tests for the Structural Research Scanner (schema v2 historical DB):
filter correctness, the no-prediction invariants, and the historical-record
machinery — persistence, observation appending, exit archiving, MFE/MAE,
descriptive statistics, and dashboard rendering.
Run: python3 test_research_scanner.py"""
import os, sys, json, traceback, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import research_scanner as R

def mk(**k):
    base = dict(symbol="KO", right="call", strike=90.0, expiration="2026-10-16",
                underlying=90.09, bid=2.36, ask=2.59, mark=2.475, iv=0.183,
                delta=0.52, theta=-0.0239, open_interest=5970, volume=151, dte=51,
                hv=None, earnings_in_window=None, days_to_earnings=None)
    base.update(k); return R.Contract(**base)

# ---------- filters + no-prediction invariants (unchanged philosophy) ----------
def test_clean_pass():
    r = R.screen(mk()); assert r["passed"]; assert r["fail_reasons"] == []
    assert abs(r["descriptors"]["break_even"] - 92.475) < 0.01

def test_over_budget():
    r = R.screen(mk(mark=3.55, bid=3.50, ask=3.60))
    assert "premium" in r["fail_reasons"] and R.exit_reason(r) == "premium >$3.00"

def test_under_floor():
    r = R.screen(mk(mark=0.60, bid=0.55, ask=0.65))
    assert "premium" in r["fail_reasons"] and R.exit_reason(r) == "premium <$0.75"

def test_wide_spread():
    r = R.screen(mk(mark=1.43, bid=1.34, ask=1.52, strike=92.5))
    assert "bid_ask" in r["fail_reasons"] and R.exit_reason(r) == "bid/ask too wide"

def test_low_oi():
    r = R.screen(mk(open_interest=969)); assert "open_interest" in r["fail_reasons"]

def test_high_theta():
    r = R.screen(mk(theta=-0.10, mark=2.0, bid=1.95, ask=2.05))
    assert "theta_burden" in r["fail_reasons"]

def test_iv_vs_hv_not_fabricated():
    assert R.screen(mk(hv=None))["descriptors"]["iv_vs_hv"] is None
    assert R.screen(mk(iv=0.30, hv=0.20))["descriptors"]["iv_vs_hv"] == "RICH"

def test_long_only():
    r = R.screen(mk(right="spread")); assert "right" in r["fail_reasons"]

def test_occ_symbol():
    assert R.occ_symbol("KO", "2026-10-16", "call", 90.0) == "KO261016C00090000"
    assert R.occ_symbol("KO", "2026-10-16", "put", 85.0) == "KO261016P00085000"
    assert R.screen(mk())["occ_symbol"] == "KO261016C00090000"

def test_no_forbidden_fields():
    r = R.screen(mk())
    for f in R.FORBIDDEN:
        assert f not in r["descriptors"], f"forbidden field {f} in descriptors"
    for k in r["descriptors"]:
        assert not any(bad in k for bad in ("score", "rank", "conviction", "alpha",
                       "expected", "probability", "recommend"))

def test_no_forbidden_in_record():
    # a full lifecycle record must contain none of the forbidden concepts as keys
    r = R.screen(mk()); card = R._new_card(r, "2026-08-27T14:05:00")
    R._archive_card(card, "premium >$3.00", "2026-09-02T14:05:00")
    keys = set()
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items(): keys.add(k.lower()); walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(card)
    for bad in ("score", "rank", "conviction", "expected_return", "win_probability", "recommendation"):
        assert bad not in keys, f"forbidden key {bad} present"

# ---------- historical record ----------
T = ["2026-08-27T14:05:00", "2026-08-28T14:05:00", "2026-08-29T14:05:00",
     "2026-08-30T14:05:00", "2026-09-01T14:05:00"]

def test_observation_appending():
    r = R.screen(mk()); cid = r["contract_id"]
    state, arch = R.update_watchlist({"active": {}}, [r], T[0])
    assert state["active"][cid]["scan_count"] == 1
    state, arch = R.update_watchlist(state, [R.screen(mk(mark=2.6, bid=2.55, ask=2.65))], T[1])
    state, arch = R.update_watchlist(state, [R.screen(mk(mark=2.7, bid=2.65, ask=2.75))], T[2])
    obs = state["active"][cid]["observations"]
    assert len(obs) == 3, "one observation appended per scan"
    assert [o["ts"] for o in obs] == T[:3], "chronological, never overwritten"
    assert obs[0]["mid"] == 2.475 and obs[2]["mid"] == 2.7
    # every required observation field present
    for f in ("ts","underlying","bid","ask","mid","premium","iv","delta","theta",
              "dte","volume","open_interest","bid_ask_pct","passed"):
        assert f in obs[0], f"observation missing {f}"

def test_historical_persistence():
    r = R.screen(mk()); cid = r["contract_id"]
    state, _ = R.update_watchlist({"active": {}}, [r], T[0])
    fd = state["active"][cid]["first_detected"]
    for t in T[1:3]:
        state, _ = R.update_watchlist(state, [R.screen(mk(mark=2.5, bid=2.45, ask=2.55))], t)
    c = state["active"][cid]
    assert c["first_detected"] == fd == T[0], "first_detected is stable"
    assert c["last_seen"] == T[2]
    assert c["days_qualified"] == 3, "distinct days counted"
    assert c["current_status"] == "ACTIVE"

def test_data_gap_does_not_exit():
    # symbol fails to fetch -> contract held, no fabricated observation, no exit
    r = R.screen(mk()); cid = r["contract_id"]
    state, _ = R.update_watchlist({"active": {}}, [r], T[0])
    state, arch = R.update_watchlist(state, [], T[1], ok_symbols=set())  # KO not in ok set
    assert cid in state["active"], "held through data gap"
    assert not arch, "not archived on a data gap"
    assert state["active"][cid]["scan_count"] == 1, "no fabricated observation"

def test_research_leaves_filter_paper_continues_then_archives():
    # DECOUPLED: leaving the research filter never closes the paper position.
    r = R.screen(mk()); cid = r["contract_id"]
    state, _ = R.update_watchlist({"active": {}}, [r], T[0], market_hours=True)   # paper enters @2.475
    assert state["active"][cid]["paper_position"]["status"] == "ACTIVE"
    # OI collapses -> research LEFT_FILTER (liquidity), premium unchanged -> paper keeps running
    state, arch = R.update_watchlist(state, [R.screen(mk(open_interest=500))], T[1], market_hours=True)
    c = state["active"][cid]
    assert c["research_status"] == "LEFT_FILTER" and "liquidity" in (c["research_left_reason"] or "")
    assert c["paper_position"]["status"] in ("ACTIVE", "TRAILING_ACTIVE"), "paper survives filter exit"
    assert not arch, "NOT archived while the paper position is still open"
    # paper then hits its own stop while research stays out of the filter -> archive BOTH histories
    state, arch = R.update_watchlist(state, [R.screen(mk(open_interest=500, mark=1.6, bid=1.55, ask=1.65))], T[2], market_hours=True)
    assert cid not in state["active"], "archived once BOTH lifecycles are terminal"
    a = arch["2026-08"][0]
    assert a["paper_position"]["exit_reason"] == "INITIAL_STOP"
    assert a["research_status"] == "LEFT_FILTER"
    assert len(a["observations"]) >= 3 and len(a["paper_position"]["observations"]) >= 3, "both histories stored together"
    assert "lifetime" in a

def test_expiry_closes_paper_and_archives():
    r = R.screen(mkp(2.0, dte=21)); cid = r["contract_id"]
    state, _ = R.update_watchlist({"active": {}}, [r], DAYS[0], market_hours=True)  # paper enters
    # a later scan past the expiration date, contract gone from the chain -> EXPIRED
    past = "2026-10-20T14:00:00+00:00"
    state, arch = R.update_watchlist(state, [], past, track_results={}, ok_symbols={"KO"}, market_hours=True)
    assert cid not in state["active"]
    a = arch["2026-10"][0]
    assert a["research_status"] == "EXPIRED" and a["paper_position"]["exit_reason"] == "EXPIRED"

def test_mfe_mae_option_and_underlying():
    # call: underlying up is favorable. Path: entry 90.09/2.475 -> 95/3.0 (up) -> 88/2.0 (down)
    r0 = R.screen(mk())  # u=90.09 mid=2.475
    state, _ = R.update_watchlist({"active": {}}, [r0], T[0])
    cid = r0["contract_id"]
    state, _ = R.update_watchlist(state, [R.screen(mk(underlying=95.0, mark=3.0, bid=2.95, ask=3.05))], T[1])
    state, _ = R.update_watchlist(state, [R.screen(mk(underlying=88.0, mark=2.0, bid=1.95, ask=2.05))], T[2])
    c = state["active"][cid]
    assert c["highest_option"] == 3.0 and c["lowest_option"] == 2.0
    assert c["highest_underlying"] == 95.0 and c["lowest_underlying"] == 88.0
    # option MFE = (3.0-2.475)/2.475*100 ~= 21.21 ; MAE = (2.0-2.475)/2.475*100 ~= -19.19
    assert abs(c["mfe_option"] - 21.21) < 0.1 and abs(c["mae_option"] + 19.19) < 0.1
    # underlying MFE (call, up favorable) = (95-90.09)/90.09*100 ~= 5.45
    #             MAE = (88-90.09)/90.09*100 ~= -2.32
    assert abs(c["mfe_underlying"] - 5.45) < 0.1 and abs(c["mae_underlying"] + 2.32) < 0.1

def test_mfe_mae_put_direction():
    # put: underlying DOWN is favorable
    r0 = R.screen(mk(right="put", strike=88.0, delta=-0.2, mark=1.5, bid=1.45, ask=1.55))
    state, _ = R.update_watchlist({"active": {}}, [r0], T[0])
    cid = r0["contract_id"]
    state, _ = R.update_watchlist(state, [R.screen(mk(right="put", strike=88.0, delta=-0.2,
                                    underlying=85.0, mark=1.5, bid=1.45, ask=1.55))], T[1])
    c = state["active"][cid]
    # underlying fell to 85 from 90.09 -> favorable for a put -> MFE positive
    assert c["mfe_underlying"] > 0 and abs(c["mfe_underlying"] - 5.65) < 0.2

def test_migration_v1_to_v2():
    # a legacy v1 active card (no observations) migrates without fabricating data
    v1 = {"contract_id": "KO 90C 2026-10-16", "symbol": "KO", "right": "call",
          "strike": 90.0, "expiration": "2026-10-16", "first_detected": T[0],
          "entry_premium": 2.475, "current_premium": 2.6, "underlying_at_detection": 90.09,
          "current_underlying": 91.0, "iv_at_detection": 0.183, "current_iv": 0.19,
          "dte": 50, "current_status": "ACTIVE", "last_updated": T[1],
          "history": [{"ts": T[0], "event": "detected", "premium": 2.475, "underlying": 90.09, "iv": 0.183, "dte": 51},
                      {"ts": T[1], "event": "still", "premium": 2.6, "underlying": 91.0, "iv": 0.19, "dte": 50}]}
    state, _ = R.update_watchlist({"active": {"KO 90C 2026-10-16": v1}},
                                  [R.screen(mk(mark=2.7, bid=2.65, ask=2.75, underlying=92.0))], T[2])
    c = state["active"]["KO 90C 2026-10-16"]
    assert c["first_detected"] == T[0], "detection preserved through migration"
    assert len(c["observations"]) == 3, "seeded 2 legacy + 1 fresh"
    assert c["observations"][0]["bid"] is None, "unknown legacy fields not fabricated"
    assert c["scan_count"] == 3 and c["mfe_underlying"] is not None

def test_descriptive_statistics():
    r = R.screen(mk()); c1 = R._new_card(r, T[0])
    R._archive_card(c1, "premium >$3.00", T[2],
                    res=R.screen(mk(mark=3.4, bid=3.35, ask=3.45, underlying=95.0)))
    c2 = R._new_card(R.screen(mk(strike=95.0, mark=1.5, bid=1.45, ask=1.55)), T[0])
    R._archive_card(c2, "DTE <21", T[3])
    s = R.descriptive_stats([c1, c2])
    assert s["count_ever_qualified"] == 2
    assert s["exit_reason_distribution"] == {"DTE <21": 1, "premium >$3.00": 1}
    assert s["avg_premium_at_detection"] is not None
    # descriptive only — no ranking / prediction keys
    for k in s:
        assert not any(bad in k for bad in ("rank", "best", "worst", "score", "predict", "expected"))

def test_full_run_split_files():
    # end-to-end: sample provider writes active file + (on synthetic exit) archive + index
    tmp = tempfile.mkdtemp()
    try:
        R.run("sample", root=tmp, now=T[0])
        wl = json.load(open(os.path.join(tmp, "data", "research_watchlist.json")))
        assert wl["meta"]["schema_version"] == R.SCHEMA_VERSION
        assert len(wl["active"]) >= 1 and "scan_log" in wl
        # the active cards carry the full v2 metric set
        any_card = next(iter(wl["active"].values()))
        for f in ("observations","days_qualified","mfe_option","mae_option",
                  "highest_option","current_option_pct","occ_symbol","last_seen"):
            assert f in any_card, f"active card missing {f}"
        assert os.path.exists(os.path.join(tmp, "data", "archive_index.json"))
    finally:
        shutil.rmtree(tmp)

def test_dashboard_rendering():
    # the shipped dashboard HTML must render the new lifecycle fields and stay non-predictive
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, "index.html"),
                  os.path.abspath(os.path.join(here, "..", "index.html"))]
    path = next((p for p in candidates if os.path.exists(p)), None)
    if path is None:
        print("    (skip dashboard test: index.html not co-located)"); return
    html = open(path, encoding="utf-8").read()
    for label in ("First Qualified", "Days Qualified", "Current Option", "Current Underlying",
                  "Highest Option", "Highest Underlying", "MFE", "MAE", "Archive", "Analytics",
                  "Paper Position", "Entry Price", "Current Stop", "Trailing", "Days Held",
                  "WAITING", "Research Lifecycle",
                  # Phase 1 per-card feed-quality fields
                  "Feed Mode", "Provider", "Quote Timestamp", "Last Update", "Observed Lag",
                  "Feed Health", "Stop Distance", "High-Water Mark", "Last Exit Eval",
                  # Phase 1 system feed-status section
                  "Data-Feed System Status", "Discovery provider", "Active-position provider",
                  "Heartbeat", "Fallback status", "Stale / disconnected",
                  # explicit waiting-position entry-eligibility diagnostics
                  "Entry Eligibility", "Waiting Reason", "Market Session", "Last Entry Eval",
                  "Latest Qualification", "Provider Quote TS", "Observed Lag"):
        assert label in html, f"dashboard missing '{label}'"
    # a stale/disconnected feed must never be able to render as normal — the class + warning exist
    assert "feed-STALE" in html and "feed-DISCONNECTED" in html, "dashboard lacks stale/disconnected feed styling"
    # The dashboard legitimately NAMES the forbidden concepts inside its non-predictive
    # disclaimers (to say it does none of them). Strip disclaimer sentences, then assert
    # the concepts never appear as functional output.
    import re as _re
    low = _re.sub(r"[^.]*\b(?:never|no ranking|no score|no rankings|no scores|not (?:a )?(?:rank|forecast)"
                  r"|documenting|does not (?:forecast|predict)|former paper-trading"
                  r"|legacy paper-trading|not part of this research terminal)[^.]*\.", " ",
                  html.lower())
    for bad in ("expected return", "win probability", "probability of profit",
                "buy signal", "sell signal", "conviction score", "alpha score"):
        assert bad not in low, f"dashboard uses forbidden concept outside a disclaimer: {bad}"

# ======================= v3: paper-position simulation =======================
POL = R.DEFAULT_POLICY
def mkp(mid, underlying=90.09, dte=51, bid=None, ask=None, right="call", strike=90.0,
        iv=0.18, delta=0.52, theta=-0.0239, oi=5970, vol=151):
    if bid is None: bid = round(mid - 0.05, 4)
    if ask is None: ask = round(mid + 0.05, 4)
    return R.Contract(symbol="KO", right=right, strike=strike, expiration="2026-10-16",
                      underlying=underlying, bid=bid, ask=ask, mark=mid, iv=iv, delta=delta,
                      theta=theta, open_interest=oi, volume=vol, dte=dte, hv=None,
                      earnings_in_window=None, days_to_earnings=None)
def pscan(state, mid, now, mh=True, **kw):
    r = R.screen(mkp(mid, **kw))
    st, arch = R.update_watchlist(state, [r], now, ok_symbols={"KO"}, policy=POL, market_hours=mh)
    return st, arch, r["contract_id"]

DAYS = ["2026-08-27T14:05:00+00:00","2026-08-28T14:05:00+00:00","2026-08-29T14:05:00+00:00",
        "2026-08-30T14:05:00+00:00","2026-08-31T14:05:00+00:00","2026-09-01T14:05:00+00:00",
        "2026-09-02T14:05:00+00:00","2026-09-03T14:05:00+00:00"]

def test_premarket_qualification_waits():
    st, arch, cid = pscan({"active": {}}, 2.0, DAYS[0], mh=False)   # qualifies OFF market hours
    pp = st["active"][cid]["paper_position"]
    assert pp["status"] == "WAITING_FOR_ENTRY" and pp["entry_ts"] is None, "no entry off-hours"

def test_first_market_hours_entry():
    st, _, cid = pscan({"active": {}}, 2.0, DAYS[0], mh=False)      # WAITING
    st, _, cid = pscan(st, 2.05, DAYS[1], mh=True)                  # first market-hours scan -> enter
    pp = st["active"][cid]["paper_position"]
    assert pp["status"] == "ACTIVE" and pp["entry_ts"] == DAYS[1]
    assert pp["entry_mid"] == 2.05 and pp["initial_stop_level"] == round(2.05*0.7,4)
    for f in ("entry_bid","entry_ask","entry_dte","entry_iv","entry_delta","entry_theta","entry_underlying"):
        assert pp[f] is not None, f"entry missing {f}"

def test_never_entered():
    st, _, cid = pscan({"active": {}}, 2.0, DAYS[0], mh=False)      # WAITING
    # next scan it no longer qualifies (premium > $3) BEFORE any market-hours entry
    r = R.screen(mkp(3.4, bid=3.35, ask=3.45))
    st, arch = R.update_watchlist(st, [r], DAYS[1], ok_symbols={"KO"}, policy=POL, market_hours=True)
    a = arch["2026-08"][0]
    assert a["paper_position"]["status"] == "NEVER_ENTERED"
    assert a["paper_position"]["exit_reason"] == "NEVER ENTERED"

# ---- entry-eligibility regression (the 10:03 ET production issue) ----
def test_1003et_run_with_15min_delayed_quote_enters():
    # REGRESSION: a workflow that executes at 10:03 ET (14:03 UTC) with a ~15-min-delayed CBOE
    # snapshot (09:48 ET) MUST enter a qualifying WAITING contract at the observed midpoint. A
    # delayed quote timestamp must NOT make the workflow 'off-hours'.
    now = "2026-08-27T14:03:00+00:00"                        # 10:03 ET (regular session)
    assert R.is_market_hours(now) is True, "10:03 ET must be market hours"
    res = R.screen(mkp(2.05)); res["provider_quote_ts"] = "2026-08-27T09:48:00"   # ~15 min delayed, today
    card = {"paper_position": R._paper_init(POL)}
    R.paper_update(card, res, now, POL, True)
    pp = card["paper_position"]
    assert pp["status"] == "ACTIVE", f"should ENTER; got {pp['status']} / {pp.get('waiting_reason')}"
    assert pp["entry_mid"] == 2.05 and pp["entry_bid"] is not None and pp["entry_ask"] is not None
    assert pp["waiting_reason"] is None
    assert pp["market_session_state"] == "REGULAR"
    assert pp["provider_quote_timestamp"] == "2026-08-27T09:48:00"
    assert 0 <= pp["observed_lag"] <= R.MAX_ENTRY_LAG_SEC     # ~900s, within tolerance
    assert pp["latest_qualification_state"] == "QUALIFIES"

def test_prior_close_quote_at_market_hours_stays_waiting_stale():
    # a frozen prior-close quote during a market-hours run must NOT enter; stay WAITING, stale reason
    now = "2026-08-27T14:03:00+00:00"                        # 10:03 ET
    res = R.screen(mkp(2.05)); res["provider_quote_ts"] = "2026-08-26T16:00:00"   # yesterday's close
    card = {"paper_position": R._paper_init(POL)}
    R.paper_update(card, res, now, POL, True)
    pp = card["paper_position"]
    assert pp["status"] == "WAITING_FOR_ENTRY", "must not enter off a stale prior-close"
    assert pp["waiting_reason"] == "WAITING — PROVIDER QUOTE STALE"
    assert pp["entry_mid"] is None, "NO entry created from yesterday's close"
    assert pp["observed_lag"] > R.MAX_ENTRY_LAG_SEC

def test_waiting_positions_always_have_a_reason():
    # off-hours qualifier: WAITING with an explicit reason + all diagnostic fields populated
    now = "2026-08-27T03:00:00+00:00"                        # 23:00 ET prior evening -> off hours
    assert R.is_market_hours(now) is False
    res = R.screen(mkp(2.05)); res["provider_quote_ts"] = "2026-08-26T16:00:00"
    card = {"paper_position": R._paper_init(POL)}
    R.paper_update(card, res, now, POL, False)
    pp = card["paper_position"]
    assert pp["status"] == "WAITING_FOR_ENTRY"
    assert pp["waiting_reason"] == "WAITING — NO MARKET-HOURS SCAN YET"
    assert pp["market_session_state"] == "CLOSED"
    assert pp["last_entry_evaluation_time"] == now
    assert pp["latest_qualification_state"] == "QUALIFIES"
    for f in ("waiting_reason","last_entry_evaluation_time","market_session_state",
              "provider_quote_timestamp","observed_lag","latest_qualification_state"):
        assert f in pp, f"waiting position missing diagnostic field {f}"

def test_initial_stop_loss():
    st, _, cid = pscan({"active": {}}, 2.0, DAYS[0], mh=True)       # enter @2.0, stop @1.4
    st, arch, cid = pscan(st, 1.40, DAYS[1], mh=True)               # -30% -> INITIAL STOP
    # paper closes but the research contract still qualifies -> stays on the active card
    pp = st["active"][cid]["paper_position"]
    assert pp["exit_reason"] == "INITIAL_STOP" and pp["status"] == "CLOSED"
    assert st["active"][cid]["current_status"] == "ACTIVE", "research contract not archived by a paper stop"

def test_trailing_activation():
    st, _, cid = pscan({"active": {}}, 2.0, DAYS[0], mh=True)
    st, _, cid = pscan(st, 2.5, DAYS[1], mh=True)                   # +25% -> activate
    pp = st["active"][cid]["paper_position"]
    assert pp["trailing_active"] and pp["status"] == "TRAILING_ACTIVE"
    assert pp["trailing_high"] == 2.5 and pp["trailing_stop_level"] == round(2.5*0.8,4)

def test_no_trailing_before_activation():
    st, _, cid = pscan({"active": {}}, 2.0, DAYS[0], mh=True)
    st, _, cid = pscan(st, 2.4, DAYS[1], mh=True)                   # +20% only -> no trailing yet
    pp = st["active"][cid]["paper_position"]
    assert not pp["trailing_active"] and pp["current_stop_level"] == pp["initial_stop_level"]

def test_trailing_high_increases_and_stop_never_decreases():
    st, _, cid = pscan({"active": {}}, 2.0, DAYS[0], mh=True)
    st, _, cid = pscan(st, 2.5, DAYS[1], mh=True)                   # activate, high 2.5, stop 2.0
    st, _, cid = pscan(st, 2.9, DAYS[2], mh=True)                   # high 2.9, stop 2.32
    pp = st["active"][cid]["paper_position"]
    assert pp["trailing_high"] == 2.9 and pp["trailing_stop_level"] == round(2.9*0.8,4)
    st, _, cid = pscan(st, 2.6, DAYS[3], mh=True)                   # price down, high & stop hold
    pp = st["active"][cid]["paper_position"]
    assert pp["trailing_high"] == 2.9, "trailing high never decreases"
    assert pp["trailing_stop_level"] == round(2.9*0.8,4), "trailing stop never decreases"

def test_gap_through_stop_and_bid_based_exit():
    st, _, cid = pscan({"active": {}}, 2.0, DAYS[0], mh=True)
    st, _, cid = pscan(st, 2.8, DAYS[1], mh=True)                   # activate, high 2.8, stop 2.24
    # gap straight down through the stop; bid distinct from mid
    st, arch, cid = pscan(st, 2.10, DAYS[2], mh=True, bid=2.00, ask=2.20)
    a = st["active"][cid]["paper_position"]
    assert a["exit_reason"] == "TRAILING_STOP"
    assert a["exit_price"] == 2.00, "conservative exit at current bid, not the stop level"
    assert "first observed" in a["exit_note"]

def test_time_stop():
    st = {"active": {}}
    st, _, cid = pscan(st, 2.0, DAYS[0], mh=True, dte=58)           # day 1
    for i, d in enumerate(DAYS[1:7], start=1):                      # days 2..7, flat price
        st, arch, _ = pscan(st, 2.0, d, mh=True, dte=58 - i)
    a = arch["2026-09"][0]["paper_position"] if arch else st["active"][cid]["paper_position"]
    assert a["exit_reason"] == "TIME_STOP" and a["days_held"] == 7

def test_dte_stop_unit():
    # DTE stop is a real path but pre-empted in integration by the DTE<21 research filter,
    # so validate the paper engine directly with a sub-14 DTE quote.
    pp = R._paper_init(POL); R._paper_enter(pp, R.screen(mkp(2.0, dte=20)), DAYS[0])
    reason = R._paper_step(pp, R.screen(mkp(2.0, dte=13)), DAYS[1])
    assert reason == "DTE_STOP" and pp["exit_reason"] == "DTE_STOP"

def test_expiration_unit():
    pp = R._paper_init(POL); R._paper_enter(pp, R.screen(mkp(2.0, dte=21)), DAYS[0])
    reason = R._paper_step(pp, R.screen(mkp(1.5, dte=0)), DAYS[1])
    assert reason == "EXPIRED"

def test_filter_exit_does_NOT_close_paper():
    st, _, cid = pscan({"active": {}}, 2.0, DAYS[0], mh=True)       # ACTIVE paper
    st, arch, cid = pscan(st, 2.0, DAYS[1], mh=True, oi=500)        # OI collapses -> research leaves filter
    c = st["active"][cid]
    assert c["research_status"] == "LEFT_FILTER"
    assert c["paper_position"]["status"] == "ACTIVE", "paper is NOT closed by leaving the filter"
    assert c["paper_position"]["exit_reason"] is None
    assert not arch, "record stays active because the paper position is still open"

def test_returned_to_filter():
    st, _, cid = pscan({"active": {}}, 2.0, DAYS[0], mh=True)       # ACTIVE, in filter
    st, _, cid = pscan(st, 2.0, DAYS[1], mh=True, oi=500)           # leaves filter
    assert st["active"][cid]["research_status"] == "LEFT_FILTER"
    st, arch, cid = pscan(st, 2.0, DAYS[2], mh=True, oi=5970)       # OI recovers -> returns to filter
    c = st["active"][cid]
    assert c["research_status"] == "ACTIVE", "research returned to the filter"
    log = [e["status"] for e in c.get("research_status_log", [])]
    assert "LEFT_FILTER" in log and "RETURNED_TO_FILTER" in log

def test_dte_stop_reachable_after_filter_exit():
    # DECOUPLING PAYOFF: a contract leaves the filter at DTE<21 but the paper keeps
    # tracking down to its own DTE<14 stop (unreachable in v3).
    st, _, cid = pscan({"active": {}}, 2.0, DAYS[0], mh=True, dte=22)   # enters, in filter
    st, arch, cid = pscan(st, 2.0, DAYS[1], mh=True, dte=18)            # DTE 18<21 -> research LEFT_FILTER, paper tracks
    assert st["active"][cid]["research_status"] == "LEFT_FILTER"
    assert st["active"][cid]["paper_position"]["status"] == "ACTIVE", "paper survived DTE filter exit"
    st, arch, cid = pscan(st, 2.0, DAYS[2], mh=True, dte=13)            # DTE 13<14 -> paper DTE_STOP
    a = arch["2026-08"][0] if arch else st["active"][cid]
    assert a["paper_position"]["exit_reason"] == "DTE_STOP", "paper DTE stop now fires post-filter"
    assert cid not in st["active"], "archived: paper closed + research out of filter"

def test_both_histories_archived_together():
    st, _, cid = pscan({"active": {}}, 2.0, DAYS[0], mh=True, dte=22)
    st, _, cid = pscan(st, 2.6, DAYS[1], mh=True, dte=18)               # research LEFT_FILTER; paper tracks up
    st, arch, cid = pscan(st, 2.0, DAYS[2], mh=True, dte=13)            # DTE_STOP closes paper -> archive
    a = arch["2026-08"][0]
    assert len(a["observations"]) >= 3, "research observation history preserved"
    assert len(a["paper_position"]["observations"]) >= 3, "paper price history preserved"
    assert a["research_status"] in ("LEFT_FILTER", "EXPIRED") and a["paper_position"]["status"] == "CLOSED"

def test_research_history_continues_after_paper_close():
    st, _, cid = pscan({"active": {}}, 2.0, DAYS[0], mh=True)       # enter
    st, _, cid = pscan(st, 1.40, DAYS[1], mh=True)                  # INITIAL STOP -> paper CLOSED
    pp = st["active"][cid]["paper_position"]
    assert pp["status"] == "CLOSED"
    research_obs_before = len(st["active"][cid]["observations"])
    st, _, cid = pscan(st, 2.0, DAYS[2], mh=True)                   # research still qualifies
    card = st["active"][cid]
    assert card["paper_position"]["status"] == "CLOSED", "paper stays closed independently"
    assert len(card["observations"]) == research_obs_before + 1, "research observations continue"

def test_policy_version_preservation():
    st, _, cid = pscan({"active": {}}, 2.0, DAYS[0], mh=True)       # entered under v1
    pol2 = dict(R.DEFAULT_POLICY); pol2["policy_version"] = "paper-policy.v2"; pol2["initial_stop_pct"] = 40.0
    r = R.screen(mkp(2.1))
    r2 = R.screen(mkp(1.5, strike=95.0))                            # a different, new contract
    st, arch = R.update_watchlist(st, [r, r2], DAYS[1], ok_symbols={"KO"}, policy=pol2, market_hours=True)
    old = st["active"][cid]["paper_position"]
    new = st["active"][r2["contract_id"]]["paper_position"]
    assert old["policy_version"] == "paper-policy.v1" and old["params"]["initial_stop_pct"] == 30.0
    assert new["policy_version"] == "paper-policy.v2" and new["params"]["initial_stop_pct"] == 40.0

def test_migration_v2_to_v3_paper():
    # a v2 active card (observations, no paper_position) enters at the next market-hours
    # scan, NOT at a fabricated historical price
    v2 = {"contract_id": "KO 90C 2026-10-16", "symbol": "KO", "right": "call", "strike": 90.0,
          "expiration": "2026-10-16", "first_detected": "2026-08-01T14:05:00+00:00",
          "entry_premium": 2.0, "current_premium": 2.0, "underlying_at_detection": 90.0,
          "current_underlying": 90.0, "iv_at_detection": 0.18, "current_iv": 0.18, "dte": 51,
          "current_status": "ACTIVE", "last_updated": "2026-08-01T14:05:00+00:00",
          "observations": [{"ts": "2026-08-01T14:05:00+00:00", "mid": 2.0, "underlying": 90.0, "dte": 51, "passed": True}],
          "history": []}
    st, _ = R.update_watchlist({"active": {"KO 90C 2026-10-16": v2}}, [R.screen(mkp(2.1))],
                               DAYS[0], ok_symbols={"KO"}, policy=POL, market_hours=True)
    c = st["active"]["KO 90C 2026-10-16"]
    assert c["first_detected"] == "2026-08-01T14:05:00+00:00", "research detection preserved"
    pp = c["paper_position"]
    assert pp["status"] == "ACTIVE" and pp["entry_ts"] == DAYS[0], "entered now, not backfilled"
    assert pp["entry_mid"] == 2.1

def test_paper_stats_descriptive():
    st, _, cid = pscan({"active": {}}, 2.0, DAYS[0], mh=True)
    st, arch, cid = pscan(st, 1.40, DAYS[1], mh=True)               # INITIAL_STOP (paper closed, research active)
    s = R.paper_stats([st["active"][cid]])
    assert s["contracts_entered"] == 1 and s["initial_stops"] == 1
    assert "exit_reason_distribution" in s and "hold_time_distribution" in s
    for k in s:
        assert not any(bad in k for bad in ("rank", "score", "win_rate", "edge", "predict", "expected"))

def test_no_forbidden_in_paper_record():
    st, _, cid = pscan({"active": {}}, 2.0, DAYS[0], mh=True)
    st, arch, cid = pscan(st, 1.40, DAYS[1], mh=True)
    keys = set()
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items(): keys.add(k.lower()); walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(st["active"][cid])
    for bad in ("score", "rank", "conviction", "expected_return", "win_probability", "recommendation", "edge"):
        assert bad not in keys, f"forbidden key {bad}"

def test_full_run_v3_writes_policy():
    import tempfile, shutil
    tmp = tempfile.mkdtemp()
    try:
        R.run("sample", root=tmp, now=DAYS[0])
        assert os.path.exists(os.path.join(tmp, "data", "research_policy.json"))
        wl = json.load(open(os.path.join(tmp, "data", "research_watchlist.json")))
        assert wl["meta"]["schema_version"] == R.SCHEMA_VERSION == "research-scanner.v3.2"
        assert "policy_version" in wl["meta"] and "paper_live" in wl["meta"] and "research_live" in wl["meta"]
        assert "feed" in wl["meta"] and wl["meta"]["feed"]["discovery_mode"] == "DELAYED"
        assert "failed_scans" in wl and "feed" in wl
        any_card = next(iter(wl["active"].values()))
        assert "paper_position" in any_card and "research_status" in any_card
        assert any_card["observations"][-1]["provider_mode"] == "DELAYED"
    finally:
        shutil.rmtree(tmp)

# ======================= Phase 1: delayed-feed timestamps & lag =======================
def test_observed_lag_calculation():
    # provider quote at 09:45:00 ET, ingested at 14:00:00 UTC (=10:00 ET) -> 15 min = 900s
    lag = R._lag_seconds("2026-08-27T09:45:00", "2026-08-27T14:00:00+00:00")
    assert lag == 900, lag
    assert R._lag_seconds(None, "2026-08-27T14:00:00+00:00") is None   # never fabricated

def test_observation_has_feed_metadata():
    c = R.Contract(symbol="KO", right="call", strike=90.0, expiration="2026-10-16",
                   underlying=90.0, bid=2.30, ask=2.40, mark=2.35, iv=0.18, delta=0.5, theta=-0.02,
                   open_interest=5000, volume=200, dte=51, hv=None, provider="cboe",
                   provider_mode="DELAYED", provider_quote_ts="2026-08-27T09:45:00",
                   option_quote_ts="2026-08-27T09:40:00")
    r = R.screen(c)
    assert r["provider"] == "cboe" and r["provider_mode"] == "DELAYED"
    assert r["provider_quote_ts"] == "2026-08-27T09:45:00"
    o = R._observation(r, "2026-08-27T14:00:00+00:00")
    for f in ("provider", "provider_mode", "provider_quote_ts", "ingestion_ts", "observed_lag_sec"):
        assert f in o, f
    assert o["provider_mode"] == "DELAYED" and o["ingestion_ts"] == "2026-08-27T14:00:00+00:00"
    assert o["observed_lag_sec"] == 900

def test_paper_observation_and_feed_health():
    # a market-hours entry carries feed metadata + a DELAYED feed_health on the position
    r = R.screen(mkp(2.0))
    st, _ = R.update_watchlist({"active": {}}, [r], "2026-08-27T14:05:00+00:00", market_hours=True)
    pp = next(iter(st["active"].values()))["paper_position"]
    assert pp["feed_mode"] == "DELAYED" and pp["feed_health"] == "DELAYED"
    assert pp["feed_provider"] == "cboe"
    assert "observed_lag_sec" in pp and "quote_ts" in pp and "last_update_ts" in pp
    assert pp["observations"][-1]["provider_mode"] == "DELAYED"

def test_delayed_labeling_never_realtime_phase1():
    # Phase 1 must NEVER label CBOE data as real-time
    import tempfile, shutil
    tmp = tempfile.mkdtemp()
    try:
        R.run("sample", root=tmp, now="2026-08-27T14:05:00+00:00")
        wl = json.load(open(os.path.join(tmp, "data", "research_watchlist.json")))
        assert wl["meta"]["feed"]["delayed"] is True
        assert wl["meta"]["feed"]["active_position_status"] == "NOT_CONFIGURED"
        assert wl["meta"]["feed"]["receiving_realtime"] == 0
        for c in wl["active"].values():
            for o in c["observations"]:
                assert o["provider_mode"] == "DELAYED", "no observation may claim REALTIME in Phase 1"
    finally:
        shutil.rmtree(tmp)

def test_failed_scan_logged_separately_and_preserves_state():
    # a total provider failure must NOT overwrite the last valid state, and logs to failed_scans
    import tempfile, shutil, json as _j
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, "data"))
        good = {"active": {"KO 90C 2026-10-16": R._new_card(R.screen(mkp(2.0)), "2026-08-27T14:00:00+00:00")},
                "last_scan": "2026-08-27T14:00:00+00:00", "meta": {"provider_status": "OK"}}
        _j.dump(good, open(os.path.join(tmp, "data", "research_watchlist.json"), "w"))
        # monkeypatch the cboe provider to always fail
        orig = R.cboe_candidates
        R.cboe_candidates = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down"))
        try:
            R.run("cboe", root=tmp, now="2026-08-27T14:05:00+00:00")
        finally:
            R.cboe_candidates = orig
        wl = _j.load(open(os.path.join(tmp, "data", "research_watchlist.json")))
        assert wl["meta"]["provider_status"] == "OFFLINE"
        assert len(wl.get("failed_scans", [])) == 1 and wl["failed_scans"][0]["status"] == "OFFLINE"
        assert "KO 90C 2026-10-16" in wl["active"], "last valid state preserved, not overwritten"
    finally:
        shutil.rmtree(tmp)

def test_fifteen_minute_schedule_in_workflow():
    # the deployed workflow must run intraday on a STAGGERED ~15-minute cadence (Phase 1 choice:
    # 15 min for delayed data, not 5) and must SKIP identical-state commits to spare Pages.
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, "..", ".github", "workflows", "research-scanner.yml"),
                  os.path.join(here, "research-scanner.yml")]
    path = next((p for p in candidates if os.path.exists(p)), None)
    if path is None:
        print("    (skip: workflow not co-located)"); return
    y = open(path).read()
    crons = [ln for ln in y.splitlines() if "cron:" in ln]
    assert crons, "workflow must define cron schedules"
    import re as _re
    mins = []
    for ln in crons:
        m = _re.search(r'cron:\s*"([^"]+)"', ln)
        if m: mins.append(m.group(1).split()[0])
    # staggered off the quarter-hour: the minute offsets must avoid :00/:15/:30/:45
    firsts = set()
    for spec in mins:
        for f in spec.split(","):
            firsts.add(f)
    assert firsts.isdisjoint({"0", "15", "30", "45"}), \
        "schedules must be staggered off the quarter-hour (:00/:15/:30/:45)"
    # a 15-min cadence: at least one hour lists >= 3 offsets spaced 15 apart (e.g. 3,18,33,48)
    assert any(len(spec.split(",")) >= 3 for spec in mins), \
        "expected a staggered 15-minute intraday cadence"
    # requirement 6: identical-state commits must be suppressed
    assert "state_fingerprint" in y, "workflow must gate commits on the state fingerprint"
    assert "skipping commit" in y.lower() or "--quiet" in y, "workflow must skip no-change commits"

def test_state_fingerprint_ignores_timestamps_but_tracks_price():
    # same meaningful state at two different times => SAME fingerprint (no churn commit);
    # a price move => DIFFERENT fingerprint (a real observation => commit).
    w1 = {"active": {"KO..C": {"research_status": "ACTIVE", "current_premium": 2.30,
                               "current_underlying": 90.0,
                               "paper_position": {"status": "ACTIVE", "current_mid": 2.30,
                                                  "current_stop_level": 1.61, "trailing_active": False}}},
          "diff": {"new": [], "exited": []}, "meta": {"total_archived": 0}}
    import copy, json as _json
    w2 = copy.deepcopy(w1)  # identical meaningful state
    w3 = copy.deepcopy(w1); w3["active"]["KO..C"]["current_premium"] = 2.55
    w3["active"]["KO..C"]["paper_position"]["current_mid"] = 2.55
    f1 = R._state_fingerprint(w1, "OK")
    f2 = R._state_fingerprint(w2, "OK")
    f3 = R._state_fingerprint(w3, "OK")
    assert f1 == f2, "identical state must fingerprint the same (skip identical commit)"
    assert f1 != f3, "a price change must change the fingerprint (commit the observation)"
    # a provider-status transition is meaningful too
    assert R._state_fingerprint(w1, "OFFLINE") != f1, "status transition must change fingerprint"

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    ok = 0
    for fn in fns:
        try:
            fn(); ok += 1; print(f"  PASS {fn.__name__}")
        except Exception:
            print(f"  FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{ok}/{len(fns)} passed")
    sys.exit(0 if ok == len(fns) else 1)
