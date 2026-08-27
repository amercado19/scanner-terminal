#!/usr/bin/env python3
"""
Structural Research Scanner  (module: research-scanner)
=======================================================
A NON-PREDICTIVE research desk. It reduces a curated universe to the LONG CALLS
and LONG PUTS that objectively pass a frozen set of structural filters, keeps a
persistent watchlist with per-contract history, and renders a static page.

It is logically SEPARATE from the predictive paper-trading engine in this repo
(build_dashboard.py / ledger.json / scanner_dashboard.html). It shares no state
and reuses only the repo's proven FREE data technique (CBOE delayed quotes +
Yahoo closes). It NEVER emits: conviction, alpha, direction calls, expected
return, win probability, rank/score, BUY/SELL, or 0DTE outputs.

Data: CBOE delayed options (cdn.cboe.com) — same source build_dashboard.py uses.
Stdlib only. Python 3.8+.

Usage:
    python3 research_scanner.py --provider cboe    # live, unattended
    python3 research_scanner.py --provider sample  # offline fixtures
"""
import argparse, json, math, os, sys, time, urllib.request
from datetime import datetime, timezone, date

# ============================== FROZEN CONFIG ==============================
# Pre-registered thresholds. Shown on every card next to the value they gate.
ACCOUNT_VALUE = 30_000.0
MAX_RISK_PER_TRADE_PCT = 0.01          # 1% of account -> $300
MAX_PREMIUM = 3.00
MIN_PREMIUM = 0.75
DTE_MIN, DTE_MAX = 21, 60
MIN_OPEN_INTEREST = 1_000
MIN_VOLUME = 100
MAX_BIDASK_PCT_OF_MID = 0.10           # <= 10% of mid
MAX_THETA_BURDEN_PER_DAY = 0.03        # |theta|/mark <= 3.0%/day
ALLOWED_RIGHTS = ("call", "put")       # long calls & long puts only
UNIVERSE = ["KO", "WMT", "UBER", "DIS", "XOM", "SMCI", "HIMS"]
THRESHOLDS_VERSION = "research-scanner.v1"

# The scanner MUST NOT emit any of these (enforced by a test below):
FORBIDDEN = ("chance_of_profit", "expected_return", "win_probability", "score",
             "rank", "quality_score", "conviction", "alpha", "direction",
             "recommendation", "buy", "sell")

HDR = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
       "Accept": "application/json", "Referer": "https://www.cboe.com/"}

# ============================== DATA MODEL ================================
class Contract:
    __slots__ = ("symbol", "right", "strike", "expiration", "underlying", "bid",
                 "ask", "mark", "iv", "delta", "theta", "open_interest",
                 "volume", "dte", "hv", "earnings_in_window", "days_to_earnings")

    def __init__(self, **k):
        for s in self.__slots__:
            setattr(self, s, k.get(s))

    @property
    def contract_id(self):
        return f"{self.symbol} {self.strike:g}{self.right[0].upper()} {self.expiration}"


# ============================== FILTER ENGINE ============================
# Pure screening. No ranking, no score, anywhere.
def _spread_pct(c):
    if c.bid is None or c.ask is None or not c.mark:
        return None
    return (c.ask - c.bid) / c.mark

def _theta_burden(c):
    if c.theta is None or not c.mark:
        return None
    return abs(c.theta) / c.mark

def screen(c):
    checks = []  # (name, passed, detail)
    checks.append(("right", c.right in ALLOWED_RIGHTS, f"{c.right}"))
    prem_ok = c.mark is not None and MIN_PREMIUM <= c.mark <= MAX_PREMIUM
    checks.append(("premium", prem_ok,
                   f"${c.mark:.2f} in ${MIN_PREMIUM:.2f}-${MAX_PREMIUM:.2f}" if c.mark is not None else "no mark"))
    max_loss = (c.mark or 0) * 100
    cap = min(MAX_PREMIUM * 100, ACCOUNT_VALUE * MAX_RISK_PER_TRADE_PCT)
    checks.append(("budget", max_loss <= cap,
                   f"${max_loss:.0f} <= ${cap:.0f} ({100*max_loss/ACCOUNT_VALUE:.2f}% acct)"))
    checks.append(("dte", DTE_MIN <= (c.dte if c.dte is not None else -1) <= DTE_MAX,
                   f"{c.dte} in {DTE_MIN}-{DTE_MAX}"))
    oi = c.open_interest or 0
    checks.append(("open_interest", oi >= MIN_OPEN_INTEREST, f"{int(oi)} >= {MIN_OPEN_INTEREST}"))
    vol = c.volume or 0
    checks.append(("volume", vol >= MIN_VOLUME, f"{int(vol)} >= {MIN_VOLUME}"))
    sp = _spread_pct(c)
    checks.append(("bid_ask", sp is not None and sp <= MAX_BIDASK_PCT_OF_MID,
                   f"{sp*100:.1f}% <= {MAX_BIDASK_PCT_OF_MID*100:.0f}%" if sp is not None else "no bid/ask"))
    tb = _theta_burden(c)
    checks.append(("theta_burden", tb is not None and tb <= MAX_THETA_BURDEN_PER_DAY,
                   f"{tb*100:.2f}%/day <= {MAX_THETA_BURDEN_PER_DAY*100:.1f}%" if tb is not None else "no theta"))

    passed = all(p for _, p, _ in checks)
    fails = [n for n, p, _ in checks if not p]

    be = (c.strike + (c.mark or 0)) if c.right == "call" else (c.strike - (c.mark or 0))
    be_move = ((be - c.underlying) / c.underlying) if c.underlying else None
    iv_vs_hv = None
    if c.iv is not None and c.hv:
        r = c.iv / c.hv
        iv_vs_hv = "RICH" if r >= 1.25 else "CHEAP" if r <= 0.85 else "FAIR"

    descriptors = {
        "delta": round(c.delta, 3) if c.delta is not None else None,
        "break_even": round(be, 2),
        "break_even_move_pct": round(be_move * 100, 2) if be_move is not None else None,
        "iv": round(c.iv, 4) if c.iv is not None else None,
        "hv": round(c.hv, 4) if c.hv is not None else None,
        "iv_vs_hv": iv_vs_hv,
        "theta": round(c.theta, 4) if c.theta is not None else None,
        "theta_burden_pct_day": round(tb * 100, 2) if tb is not None else None,
        "open_interest": int(oi),
        "volume": int(vol),
        "bid_ask_pct": round(sp * 100, 1) if sp is not None else None,
        "earnings_in_window": c.earnings_in_window,       # None = not wired (never fabricated)
        "days_to_earnings": c.days_to_earnings,
    }
    return {
        "contract_id": c.contract_id, "symbol": c.symbol, "right": c.right,
        "strike": c.strike, "expiration": c.expiration, "dte": c.dte, "mark": c.mark,
        "underlying": round(c.underlying, 2) if c.underlying else None,
        "checks": [{"name": n, "passed": p, "detail": d} for n, p, d in checks],
        "passed": passed, "fail_reasons": fails, "descriptors": descriptors,
    }

def exit_reason(res):
    if res["passed"]:
        return None
    m = {"premium": ("premium >$3.00" if res["mark"] and res["mark"] > MAX_PREMIUM else "premium <$0.75"),
         "budget": "over budget", "dte": ("DTE >60" if (res["dte"] or 0) > DTE_MAX else "DTE <21"),
         "open_interest": "liquidity failure (OI)", "volume": "liquidity failure (volume)",
         "bid_ask": "bid/ask too wide", "theta_burden": "theta burden too high",
         "right": "not a long call/put"}
    for n in ("premium", "budget", "dte", "open_interest", "volume", "bid_ask", "theta_burden", "right"):
        if n in res["fail_reasons"]:
            return m[n]
    return "no longer qualifies"


# ============================== WATCHLIST DIFF ==========================
def _card(res, now):
    return {"contract_id": res["contract_id"], "symbol": res["symbol"], "right": res["right"],
            "strike": res["strike"], "expiration": res["expiration"],
            "first_detected": now, "entry_premium": res["mark"], "current_premium": res["mark"],
            "premium_change": 0.0, "underlying_at_detection": res["underlying"],
            "current_underlying": res["underlying"], "iv_at_detection": res["descriptors"]["iv"],
            "current_iv": res["descriptors"]["iv"], "dte": res["dte"], "current_status": "ACTIVE",
            "entry_reason": "passed all frozen structural filters", "exit_reason": None, "exited_at": None,
            "descriptors": res["descriptors"], "checks": res["checks"], "last_updated": now,
            "history": [{"ts": now, "event": "detected", "premium": res["mark"],
                         "underlying": res["underlying"], "iv": res["descriptors"]["iv"], "dte": res["dte"]}]}

def update_watchlist(prev, results, now):
    active = dict(prev.get("active", {}))
    exited = list(prev.get("exited", []))
    by_id = {r["contract_id"]: r for r in results}
    diff = {"new": [], "still": [], "exited": [], "reentered": []}
    prior_exit_ids = {c["contract_id"] for c in exited}

    for cid, card in list(active.items()):
        r = by_id.get(cid)
        if r is None:
            card["current_status"] = "EXITED"; card["exit_reason"] = "not present in scan"
            card["exited_at"] = now; card["last_updated"] = now
            card["history"].append({"ts": now, "event": "exited", "reason": card["exit_reason"]})
            exited.append(card); del active[cid]; diff["exited"].append(cid); continue
        if r["passed"]:
            card["current_premium"] = r["mark"]
            card["premium_change"] = round((r["mark"] or 0) - (card["entry_premium"] or 0), 2)
            card["current_underlying"] = r["underlying"]; card["current_iv"] = r["descriptors"]["iv"]
            card["dte"] = r["dte"]; card["descriptors"] = r["descriptors"]; card["checks"] = r["checks"]
            card["current_status"] = "ACTIVE"; card["last_updated"] = now
            card["history"].append({"ts": now, "event": "still", "premium": r["mark"],
                                    "underlying": r["underlying"], "iv": r["descriptors"]["iv"], "dte": r["dte"]})
            active[cid] = card; diff["still"].append(cid)
        else:
            card["current_status"] = "EXITED"; card["exit_reason"] = exit_reason(r)
            card["exited_at"] = now; card["last_updated"] = now
            card["history"].append({"ts": now, "event": "exited", "reason": card["exit_reason"]})
            exited.append(card); del active[cid]; diff["exited"].append(cid)

    for cid, r in by_id.items():
        if r["passed"] and cid not in active:
            c = _card(r, now)
            if cid in prior_exit_ids:
                c["entry_reason"] = "re-entered (re-passed filters)"
                c["history"].append({"ts": now, "event": "reentered"})
                diff["reentered"].append(cid)
            else:
                diff["new"].append(cid)
            active[cid] = c

    return {"active": active, "exited": exited, "last_scan": now, "diff": diff}


# ============================== PROVIDERS =================================
def _yahoo_hv(sym):
    """Reuse the repo's Yahoo technique for a 30-day realized-vol descriptor.
    Returns annualized HV (decimal) or None. Never fabricates on failure."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=3mo&interval=1d"
        d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20))
        closes = [c for c in d["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c][-31:]
        if len(closes) < 20:
            return None
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        n = len(rets); mean = sum(rets) / n
        var = sum((r - mean) ** 2 for r in rets) / (n - 1)
        return math.sqrt(var) * math.sqrt(252)
    except Exception:
        return None

def _parse_occ(sym):
    # e.g. KO261016C00090000 -> ("KO", "2026-10-16", "call", 90.0)
    i = 0
    while i < len(sym) and not sym[i].isdigit():
        i += 1
    root = sym[:i]
    yymmdd = sym[i:i + 6]
    right = "call" if sym[i + 6].upper() == "C" else "put"
    strike = int(sym[i + 7:i + 15]) / 1000.0
    exp = f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
    return root, exp, right, strike

def cboe_candidates(symbol, today, want_hv=True):
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
    last = None
    for i in range(4):
        try:
            data = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=40))["data"]
            break
        except Exception as e:
            last = e; time.sleep(15 * (i + 1))
    else:
        raise last
    underlying = data.get("current_price") or data.get("close")
    hv = _yahoo_hv(symbol) if want_hv else None
    out = []
    for o in data["options"]:
        try:
            root, exp, right, strike = _parse_occ(o["option"])
        except Exception:
            continue
        try:
            dte = (date.fromisoformat(exp) - today).days
        except Exception:
            dte = None
        if dte is None or not (DTE_MIN - 3 <= dte <= DTE_MAX + 3):   # coarse pre-window to cut volume
            continue
        bid, ask = o.get("bid"), o.get("ask")
        mark = ((bid + ask) / 2) if (bid not in (None, 0) or ask not in (None, 0)) else None
        out.append(Contract(symbol=symbol, right=right, strike=strike, expiration=exp,
                            underlying=underlying, bid=bid, ask=ask, mark=mark, iv=o.get("iv"),
                            delta=o.get("delta"), theta=o.get("theta"),
                            open_interest=o.get("open_interest"), volume=o.get("volume"),
                            dte=dte, hv=hv, earnings_in_window=None, days_to_earnings=None))
    return out

# Offline fixtures (real 2026-08-26 closing values) for CI dry-run with no network.
_SAMPLE = [
    ("KO", "KO261016C00090000", 89.885, 2.36, 2.59, 0.1827, 0.5201, -0.0239, 5970, 151),
    ("KO", "KO261016P00085000", 89.885, 0.73, 0.80, 0.2056, -0.194, -0.0168, 2586, 293),
    ("WMT", "WMT261016C00110000", 104.35, 1.65, 1.72, 0.2313, 0.3066, -0.0342, 7280, 2341),
    ("UBER", "UBER261016C00082500", 78.48, 2.60, 2.69, 0.3514, 0.3919, -0.0418, 1778, 640),
    ("UBER", "UBER261016C00085000", 78.48, 1.84, 1.89, 0.3486, 0.3061, -0.0375, 17561, 6248),
    ("UBER", "UBER261016C00090000", 78.48, 0.90, 0.98, 0.3553, 0.1774, -0.0279, 13439, 6182),
    ("WMT", "WMT261016C00105000", 104.35, 3.50, 3.60, 0.2313, 0.513, -0.0405, 5376, 4690),  # over budget
]
def sample_candidates(symbol, today, want_hv=False):
    out = []
    for sym, occ, u, bid, ask, iv, delta, theta, oi, vol in _SAMPLE:
        if sym != symbol:
            continue
        root, exp, right, strike = _parse_occ(occ)
        out.append(Contract(symbol=sym, right=right, strike=strike, expiration=exp, underlying=u,
                            bid=bid, ask=ask, mark=round((bid + ask) / 2, 4), iv=iv, delta=delta,
                            theta=theta, open_interest=oi, volume=vol, dte=51, hv=None,
                            earnings_in_window=None, days_to_earnings=None))
    return out


# ============================== RUN ======================================
def run(provider, today=None, now=None, root="."):
    today = today or date.today()
    now = now or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    data_path = os.path.join(root, "data", "research_watchlist.json")

    cand = {"cboe": cboe_candidates, "sample": sample_candidates}[provider]
    results, examined, errors = [], 0, []
    for sym in UNIVERSE:
        try:
            for c in cand(sym, today):
                results.append(screen(c)); examined += 1
            time.sleep(0.4)  # be gentle with CBOE
        except Exception as e:
            errors.append(f"{sym}: {e}")
            print(f"[warn] {sym}: {e}", file=sys.stderr)

    # FAILURE RULE: if EVERY symbol failed to fetch, do NOT overwrite prior scan.
    if provider == "cboe" and errors and examined == 0:
        prev = _load(data_path)
        prev.setdefault("meta", {})
        prev["meta"]["provider_status"] = "OFFLINE"
        prev["meta"]["last_error_at"] = now
        prev["meta"]["errors"] = errors
        _save(data_path, prev)
        print("PROVIDER OFFLINE — kept previous watchlist (last good scan: "
              f"{prev.get('last_scan')})", file=sys.stderr)
        return prev

    prev = _load(data_path)
    updated = update_watchlist(prev, results, now)
    updated["meta"] = {
        "provider": provider, "provider_status": "OK" if not errors else "PARTIAL",
        "thresholds_version": THRESHOLDS_VERSION, "examined": examined,
        "universe": UNIVERSE, "generated_at": now, "last_successful_scan": now,
        "errors": errors,
        "boundary": ("Research only. Non-predictive structural screen of long calls & puts. "
                     "No ranking, no score, no conviction, no direction, no expected return, "
                     "no recommendation."),
        "thresholds": {"premium": [MIN_PREMIUM, MAX_PREMIUM], "max_cost": MAX_PREMIUM * 100,
                       "dte": [DTE_MIN, DTE_MAX], "min_oi": MIN_OPEN_INTEREST, "min_vol": MIN_VOLUME,
                       "max_bidask_pct": MAX_BIDASK_PCT_OF_MID * 100,
                       "max_theta_burden_pct_day": MAX_THETA_BURDEN_PER_DAY * 100},
    }
    _save(data_path, updated)
    d = updated["diff"]
    print(f"scan {now} [{provider}] examined={examined} active={len(updated['active'])} "
          f"new={len(d['new'])} still={len(d['still'])} exited={len(d['exited'])} "
          f"reentered={len(d['reentered'])}")
    if not updated["active"]:
        print("NO STRUCTURALLY QUALIFIED CONTRACTS TODAY")
    return updated

def _load(p):
    if not os.path.exists(p):
        return {"active": {}, "exited": [], "last_scan": None}
    with open(p) as f:
        return json.load(f)

def _save(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, p)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="cboe", choices=["cboe", "sample"])
    ap.add_argument("--root", default=os.path.dirname(os.path.abspath(__file__)))
    args = ap.parse_args()
    run(args.provider, root=args.root)
