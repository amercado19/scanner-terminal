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

def test_exit_archiving():
    r = R.screen(mk()); cid = r["contract_id"]
    state, _ = R.update_watchlist({"active": {}}, [r], T[0])
    # premium jumps above $3 -> structural fail -> archived, not deleted
    state, arch = R.update_watchlist(state, [R.screen(mk(mark=3.4, bid=3.35, ask=3.45))], T[1])
    assert cid not in state["active"], "removed from active on exit"
    month = "2026-08"
    assert month in arch and arch[month][0]["contract_id"] == cid
    a = arch[month][0]
    assert a["current_status"] == "EXITED" and a["exit_reason"] == "premium >$3.00"
    assert a["exited_at"] == T[1] and a["observations"][-1]["mid"] == 3.4, "final observation appended"
    assert "lifetime" in a and a["lifetime"]["exit_reason"] == "premium >$3.00"

def test_expiry_and_dte_exit():
    r = R.screen(mk()); cid = r["contract_id"]
    state, _ = R.update_watchlist({"active": {}}, [r], T[0])
    # contract absent from results, symbol fetched OK, not past expiry -> DTE <21
    state, arch = R.update_watchlist(state, [], T[1], ok_symbols={"KO"})
    assert cid not in state["active"] and arch["2026-08"][0]["exit_reason"] == "DTE <21"

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
                  "Highest Option", "Highest Underlying", "MFE", "MAE", "Archive", "Analytics"):
        assert label in html, f"dashboard missing '{label}'"
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
