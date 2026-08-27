#!/usr/bin/env python3
"""Tests for the Structural Research Scanner: filter correctness + the
no-prediction invariants. Run: python3 test_research_scanner.py"""
import os, sys, json, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import research_scanner as R

def mk(**k):
    base = dict(symbol="KO", right="call", strike=90.0, expiration="2026-10-16",
                underlying=90.09, bid=2.36, ask=2.59, mark=2.475, iv=0.183,
                delta=0.52, theta=-0.0239, open_interest=5970, volume=151, dte=51,
                hv=None, earnings_in_window=None, days_to_earnings=None)
    base.update(k); return R.Contract(**base)

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
    r = R.screen(mk(open_interest=969))
    assert "open_interest" in r["fail_reasons"]

def test_high_theta():
    r = R.screen(mk(theta=-0.10, mark=2.0, bid=1.95, ask=2.05))
    assert "theta_burden" in r["fail_reasons"]

def test_iv_vs_hv_not_fabricated():
    assert R.screen(mk(hv=None))["descriptors"]["iv_vs_hv"] is None
    assert R.screen(mk(iv=0.30, hv=0.20))["descriptors"]["iv_vs_hv"] == "RICH"

def test_no_forbidden_fields():
    r = R.screen(mk())
    blob = json.dumps(r).lower()
    for f in R.FORBIDDEN:
        assert f not in r["descriptors"], f"forbidden field {f} in descriptors"
    # descriptors carry no score/rank/prediction keys
    for k in r["descriptors"]:
        assert not any(bad in k for bad in ("score", "rank", "conviction", "alpha",
                       "expected", "probability", "recommend"))

def test_watchlist_new_still_exit_reenter():
    t0, t1, t2, t3 = "2026-08-26T20:00:00", "2026-08-27T20:00:00", "2026-08-28T20:00:00", "2026-08-29T20:00:00"
    r = R.screen(mk()); cid = r["contract_id"]
    w = R.update_watchlist({"active": {}, "exited": []}, [r], t0)
    assert w["diff"]["new"] == [cid] and w["active"][cid]["first_detected"] == t0
    w = R.update_watchlist(w, [R.screen(mk(mark=2.1, bid=2.05, ask=2.15))], t1)
    assert w["diff"]["still"] == [cid] and w["active"][cid]["first_detected"] == t0
    w = R.update_watchlist(w, [R.screen(mk(mark=3.4, bid=3.35, ask=3.45))], t2)
    assert cid not in w["active"] and w["exited"][-1]["exit_reason"] == "premium >$3.00"
    # re-entry: passes again -> flagged reentered, history preserved
    w = R.update_watchlist(w, [R.screen(mk())], t3)
    assert cid in w["active"] and w["diff"]["reentered"] == [cid]

def test_long_only():
    # right must be call/put; anything else fails
    r = R.screen(mk(right="spread"))
    assert "right" in r["fail_reasons"]

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
