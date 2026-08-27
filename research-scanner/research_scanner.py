#!/usr/bin/env python3
"""
Structural Research Scanner  (module: research-scanner)  —  schema v2 (historical DB)
=====================================================================================
A NON-PREDICTIVE research desk. It reduces a curated universe to the LONG CALLS
and LONG PUTS that objectively pass a frozen set of structural filters, and then
builds a permanent HISTORICAL RECORD for every contract that ever qualifies:
one observation appended on every successful scan, running lifecycle metrics
(MFE/MAE, high/low, % change), and permanent archival on exit — nothing is ever
deleted.

Philosophy is unchanged and enforced by tests: it NEVER ranks, scores, predicts
direction, recommends, or computes expected return / win probability. It only
documents, structurally, what happened after a contract entered the filter set.

Storage (split, partitioned, atomic):
    data/research_watchlist.json           active/new/re-entered contracts + full
                                           observations while active + scan_log
    data/archive_index.json                index of archive partitions
    data/archive/YYYY/YYYY-MM.json         exited contracts (full history + lifetime
                                           stats), partitioned by exit month

Data: CBOE delayed options (cdn.cboe.com). Stdlib only. Python 3.8+.

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
SCHEMA_VERSION = "research-scanner.v2"
SCAN_LOG_KEEP = 200                     # rolling scan_log entries kept in active file

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


def occ_symbol(symbol, expiration, right, strike):
    """Build the OCC option symbol, e.g. ('KO','2026-10-16','call',90.0) -> KO261016C00090000."""
    try:
        y, m, d = expiration.split("-")
        yymmdd = f"{y[2:]}{m}{d}"
        cp = "C" if right == "call" else "P"
        strike_int = int(round(float(strike) * 1000))
        return f"{symbol}{yymmdd}{cp}{strike_int:08d}"
    except Exception:
        return None


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
        "bid": c.bid, "ask": c.ask,
        "occ_symbol": occ_symbol(c.symbol, c.expiration, c.right, c.strike),
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


# ============================== HISTORICAL RECORD =======================
def _observation(res, now):
    """One point on a contract's timeline. Only real, scanned values — never fabricated."""
    d = res["descriptors"]
    return {
        "ts": now,
        "underlying": res.get("underlying"),
        "bid": res.get("bid"), "ask": res.get("ask"), "mid": res.get("mark"),
        "premium": res.get("mark"),
        "iv": d.get("iv"), "delta": d.get("delta"), "theta": d.get("theta"),
        "dte": res.get("dte"), "volume": d.get("volume"),
        "open_interest": d.get("open_interest"), "bid_ask_pct": d.get("bid_ask_pct"),
        "passed": bool(res.get("passed")),
    }

def _pct(cur, base):
    if cur is None or base in (None, 0):
        return None
    return round((cur - base) / base * 100, 2)

def _recompute(card):
    """Recompute all running lifecycle metrics from the full observation list.
    Descriptive only: high/low, % change since detection, and MFE/MAE relative to
    the contract's OWN price path and its structural exposure. No forecasting."""
    obs = card.get("observations", [])
    u0 = card.get("underlying_at_detection")
    p0 = card.get("entry_premium")
    right = card.get("right")

    u_vals = [o["underlying"] for o in obs if o.get("underlying") is not None]
    p_vals = [o["mid"] for o in obs if o.get("mid") is not None]

    cur_u = u_vals[-1] if u_vals else card.get("current_underlying")
    cur_p = p_vals[-1] if p_vals else card.get("current_premium")

    card["highest_underlying"] = max(u_vals) if u_vals else None
    card["lowest_underlying"] = min(u_vals) if u_vals else None
    card["highest_option"] = max(p_vals) if p_vals else None
    card["lowest_option"] = min(p_vals) if p_vals else None

    card["current_underlying_pct"] = _pct(cur_u, u0)
    card["current_option_pct"] = _pct(cur_p, p0)
    card["highest_underlying_pct"] = _pct(card["highest_underlying"], u0)
    card["lowest_underlying_pct"] = _pct(card["lowest_underlying"], u0)
    card["highest_option_pct"] = _pct(card["highest_option"], p0)
    card["lowest_option_pct"] = _pct(card["lowest_option"], p0)

    # Option MFE/MAE = best / worst unrealized P&L of holding the long option (its own path).
    card["mfe_option"] = card["highest_option_pct"]
    card["mae_option"] = card["lowest_option_pct"]

    # Underlying MFE/MAE = excursion relative to THIS contract's structural exposure
    # (a call gains when the underlying rises, a put when it falls). Descriptive of the
    # realized path — not a claim about direction.
    if u0 not in (None, 0) and u_vals and right in ("call", "put"):
        rel = []
        for u in u_vals:
            move = (u - u0) / u0
            rel.append(move if right == "call" else -move)
        card["mfe_underlying"] = round(max(rel) * 100, 2)
        card["mae_underlying"] = round(min(rel) * 100, 2)
    else:
        card["mfe_underlying"] = None
        card["mae_underlying"] = None

    # days qualified = distinct UTC dates on which an observation exists
    days = {o["ts"][:10] for o in obs if o.get("ts")}
    card["days_qualified"] = len(days)
    card["scan_count"] = len(obs)
    return card

def _new_card(res, now, reentered=False):
    card = {
        # identity
        "contract_id": res["contract_id"], "symbol": res["symbol"], "right": res["right"],
        "strike": res["strike"], "expiration": res["expiration"],
        "occ_symbol": res.get("occ_symbol"),
        # detection
        "first_detected": now, "last_seen": now, "last_updated": now,
        "current_status": "NEW",
        "entry_reason": "re-entered (re-passed filters)" if reentered else "passed all frozen structural filters",
        # entry snapshot
        "entry_premium": res["mark"], "underlying_at_detection": res["underlying"],
        "iv_at_detection": res["descriptors"]["iv"],
        # current
        "current_premium": res["mark"], "current_underlying": res["underlying"],
        "current_iv": res["descriptors"]["iv"], "dte": res["dte"], "premium_change": 0.0,
        # exit (null while active)
        "exit_reason": None, "exited_at": None,
        # structural snapshot (latest)
        "descriptors": res["descriptors"], "checks": res["checks"],
        # full timeline
        "observations": [_observation(res, now)],
        # legacy mirror (kept small for backward-compatible dashboards)
        "history": [{"ts": now, "event": "reentered" if reentered else "detected",
                     "premium": res["mark"], "underlying": res["underlying"],
                     "iv": res["descriptors"]["iv"], "dte": res["dte"]}],
    }
    return _recompute(card)

def _touch_active(card, res, now):
    """Append an observation and refresh current + derived metrics for a still-qualifying contract."""
    card["observations"].append(_observation(res, now))
    card["current_premium"] = res["mark"]
    card["premium_change"] = round((res["mark"] or 0) - (card["entry_premium"] or 0), 2)
    card["current_underlying"] = res["underlying"]
    card["current_iv"] = res["descriptors"]["iv"]
    card["dte"] = res["dte"]
    card["descriptors"] = res["descriptors"]
    card["checks"] = res["checks"]
    card["current_status"] = "ACTIVE"
    card["last_seen"] = now
    card["last_updated"] = now
    card["history"].append({"ts": now, "event": "still", "premium": res["mark"],
                            "underlying": res["underlying"], "iv": res["descriptors"]["iv"], "dte": res["dte"]})
    return _recompute(card)

def _lifetime(card):
    """Freeze descriptive lifetime statistics at exit. No ranking, no edge claim."""
    obs = card.get("observations", [])
    p0 = card.get("entry_premium")
    u0 = card.get("underlying_at_detection")
    p_last = card.get("current_premium")
    u_last = card.get("current_underlying")
    return {
        "days_qualified": card.get("days_qualified"),
        "scan_count": card.get("scan_count"),
        "first_detected": card.get("first_detected"),
        "exited_at": card.get("exited_at"),
        "exit_reason": card.get("exit_reason"),
        "premium_at_detection": p0,
        "premium_at_exit": p_last,
        "underlying_at_detection": u0,
        "underlying_at_exit": u_last,
        "option_return_pct": _pct(p_last, p0),
        "underlying_return_pct": _pct(u_last, u0),
        "highest_option_pct": card.get("highest_option_pct"),
        "largest_option_drawdown_pct": card.get("lowest_option_pct"),
        "highest_underlying_pct": card.get("highest_underlying_pct"),
        "largest_underlying_drawdown_pct": card.get("lowest_underlying_pct"),
        "mfe_option": card.get("mfe_option"), "mae_option": card.get("mae_option"),
        "mfe_underlying": card.get("mfe_underlying"), "mae_underlying": card.get("mae_underlying"),
    }

def _archive_card(card, reason, now, res=None):
    """Finalize a contract for permanent archival. Appends a final real observation
    when fresh data exists; never fabricates one when data is unavailable."""
    if res is not None:
        card["observations"].append(_observation(res, now))
        card["current_premium"] = res["mark"]
        card["current_underlying"] = res["underlying"]
        card["current_iv"] = res["descriptors"]["iv"]
        card["dte"] = res["dte"]
        card["descriptors"] = res["descriptors"]
        card["checks"] = res["checks"]
        card["last_seen"] = now
    _recompute(card)
    card["current_status"] = "EXITED"
    card["exit_reason"] = reason
    card["exited_at"] = now
    card["last_updated"] = now
    card["history"].append({"ts": now, "event": "exited", "reason": reason})
    card["lifetime"] = _lifetime(card)
    return card


# ============================== MIGRATION (v1 -> v2) ====================
def _migrate_card(card, now):
    """Bring a legacy v1 active card up to the v2 historical schema, in place,
    without inventing data. Seeds observations from the legacy history when needed."""
    if "observations" in card and card.get("current_status") in ("NEW", "ACTIVE", "EXITED"):
        # already v2-shaped; still ensure derived metrics exist
        card.setdefault("last_seen", card.get("last_updated") or now)
        return _recompute(card)
    # seed observations from legacy history (partial fields; unknowns stay null)
    obs = []
    for h in card.get("history", []):
        if h.get("event") in ("detected", "still", "reentered") or ("premium" in h):
            obs.append({
                "ts": h.get("ts"), "underlying": h.get("underlying"),
                "bid": None, "ask": None, "mid": h.get("premium"), "premium": h.get("premium"),
                "iv": h.get("iv"), "delta": None, "theta": None, "dte": h.get("dte"),
                "volume": None, "open_interest": None, "bid_ask_pct": None,
                "passed": True,
            })
    if not obs:
        obs.append({
            "ts": card.get("first_detected") or now,
            "underlying": card.get("underlying_at_detection"),
            "bid": None, "ask": None, "mid": card.get("entry_premium"),
            "premium": card.get("entry_premium"), "iv": card.get("iv_at_detection"),
            "delta": None, "theta": None, "dte": card.get("dte"),
            "volume": None, "open_interest": None, "bid_ask_pct": None, "passed": True,
        })
    card["observations"] = obs
    card.setdefault("occ_symbol", occ_symbol(card.get("symbol"), card.get("expiration"),
                                             card.get("right"), card.get("strike")))
    card.setdefault("last_seen", card.get("last_updated") or now)
    card.setdefault("current_status", "ACTIVE")
    card.setdefault("entry_reason", "passed all frozen structural filters")
    return _recompute(card)


# ============================== ARCHIVE INDEX / PARTITIONS ==============
def _empty_index(now):
    return {"schema_version": SCHEMA_VERSION, "updated_at": now, "total_archived": 0,
            "tickers": [], "seen_contract_ids": [], "partitions": [], "stats": None}

def descriptive_stats(contracts):
    """Descriptive-only summary of a set of archived contracts. Counts, averages, and
    an exit-reason distribution. Deliberately NO ranking, NO best/worst, NO forecast,
    NO edge claim — it only reports what the historical record already contains."""
    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None
    lifes = [c.get("lifetime", {}) for c in contracts]
    dist = {}
    for c in contracts:
        r = c.get("exit_reason") or "unknown"
        dist[r] = dist.get(r, 0) + 1
    return {
        "count_ever_qualified": len(contracts),
        "avg_days_qualified": avg([l.get("days_qualified") for l in lifes]),
        "avg_option_return_pct": avg([l.get("option_return_pct") for l in lifes]),
        "avg_underlying_move_pct": avg([l.get("underlying_return_pct") for l in lifes]),
        "avg_premium_at_detection": avg([l.get("premium_at_detection") for l in lifes]),
        "avg_premium_at_exit": avg([l.get("premium_at_exit") for l in lifes]),
        "exit_reason_distribution": dict(sorted(dist.items())),
    }

def _partition_rel(month):          # month == 'YYYY-MM'
    return os.path.join("archive", month[:4], f"{month}.json")

def _reindex(index, months_touched, data_dir, now):
    """Rebuild index rows for the touched partitions by reading them back."""
    by_month = {p["month"]: p for p in index.get("partitions", [])}
    all_tickers = set(index.get("tickers", []))
    seen_ids = set(index.get("seen_contract_ids", []))
    for month in months_touched:
        path = os.path.join(data_dir, _partition_rel(month))
        try:
            with open(path) as f:
                part = json.load(f)
        except Exception:
            continue
        contracts = part.get("contracts", [])
        tks = sorted({c.get("symbol") for c in contracts if c.get("symbol")})
        exits = sorted([c.get("exited_at") for c in contracts if c.get("exited_at")])
        by_month[month] = {"month": month, "path": _partition_rel(month),
                           "count": len(contracts), "tickers": tks,
                           "date_range": [exits[0], exits[-1]] if exits else [None, None]}
        all_tickers.update(tks)
        seen_ids.update(c.get("contract_id") for c in contracts if c.get("contract_id"))
    index["partitions"] = [by_month[m] for m in sorted(by_month)]
    index["total_archived"] = sum(p["count"] for p in index["partitions"])
    index["tickers"] = sorted(all_tickers)
    index["seen_contract_ids"] = sorted(seen_ids)
    # recompute descriptive stats across ALL archived contracts (small volume)
    all_contracts = []
    for p in index["partitions"]:
        try:
            with open(os.path.join(data_dir, p["path"])) as f:
                all_contracts.extend(json.load(f).get("contracts", []))
        except Exception:
            pass
    index["stats"] = descriptive_stats(all_contracts)
    index["updated_at"] = now
    return index


# ============================== SCAN UPDATE ============================
def update_watchlist(prev, results, now, ok_symbols=None, seen_ids=None):
    """Advance the historical record by one scan.

    prev        : previous active-file state (v1 or v2)
    results     : list of screen() dicts for everything examined this scan
    ok_symbols  : set of symbols that fetched successfully this scan (None => all ok).
                  Contracts whose symbol failed to fetch are held, not exited, and
                  get NO fabricated observation.
    seen_ids    : set of contract_ids ever archived (for re-entry flagging)

    Returns (active_state_dict, {month: [archived_card,...]})
    """
    active = {cid: _migrate_card(dict(c), now) for cid, c in prev.get("active", {}).items()}
    by_id = {r["contract_id"]: r for r in results}
    diff = {"new": [], "still": [], "exited": [], "reentered": []}
    archived = {}
    seen_ids = set(seen_ids or [])
    ok = None if ok_symbols is None else set(ok_symbols)

    def archive(card, reason, res=None):
        _archive_card(card, reason, now, res)
        month = (card.get("exited_at") or now)[:7]
        archived.setdefault(month, []).append(card)
        diff["exited"].append(card["contract_id"])

    for cid, card in list(active.items()):
        r = by_id.get(cid)
        if r is not None:
            if r["passed"]:
                _touch_active(card, r, now); diff["still"].append(cid)
            else:
                archive(card, exit_reason(r), res=r); del active[cid]
            continue
        # not in this scan's results
        sym = card.get("symbol")
        if ok is not None and sym not in ok:
            # data gap for this symbol — hold the contract, do NOT fabricate/exit
            card["last_updated"] = now
            continue
        # symbol fetched fine but contract absent -> below pre-window / expired / delisted
        try:
            expired = date.fromisoformat(card.get("expiration")) < (date.fromisoformat(now[:10]))
        except Exception:
            expired = False
        reason = "Expired" if expired else "DTE <21"
        archive(card, reason, res=None); del active[cid]

    for cid, r in by_id.items():
        if r["passed"] and cid not in active:
            reentered = cid in seen_ids
            active[cid] = _new_card(r, now, reentered=reentered)
            (diff["reentered"] if reentered else diff["new"]).append(cid)

    return {"active": active, "last_scan": now, "diff": diff}, archived


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
    data_dir = os.path.join(root, "data")
    data_path = os.path.join(data_dir, "research_watchlist.json")
    index_path = os.path.join(data_dir, "archive_index.json")

    cand = {"cboe": cboe_candidates, "sample": sample_candidates}[provider]
    results, examined, errors, ok_symbols = [], 0, [], set()
    for sym in UNIVERSE:
        try:
            got = 0
            for c in cand(sym, today):
                results.append(screen(c)); examined += 1; got += 1
            ok_symbols.add(sym)
            time.sleep(0.4)  # be gentle with CBOE
        except Exception as e:
            errors.append(f"{sym}: {e}")
            print(f"[warn] {sym}: {e}", file=sys.stderr)

    prev = _load(data_path)
    index = _load_index(index_path, now)

    # FAILURE RULE: if EVERY symbol failed to fetch, do NOT advance the record.
    if provider == "cboe" and errors and examined == 0:
        prev.setdefault("meta", {})
        prev["meta"]["provider_status"] = "OFFLINE"
        prev["meta"]["last_error_at"] = now
        prev["meta"]["errors"] = errors
        prev.setdefault("scan_log", [])
        prev["scan_log"].append({"ts": now, "status": "OFFLINE", "examined": 0, "errors": errors})
        prev["scan_log"] = prev["scan_log"][-SCAN_LOG_KEEP:]
        _save(data_path, prev)
        print("PROVIDER OFFLINE — kept previous watchlist (last good scan: "
              f"{prev.get('last_scan')})", file=sys.stderr)
        return prev

    seen_ids = set(index.get("seen_contract_ids", []))
    state, archived = update_watchlist(prev, results, now, ok_symbols=ok_symbols, seen_ids=seen_ids)

    # write archive partitions (append-only), then reindex
    months_touched = []
    for month, cards in archived.items():
        _append_partition(data_dir, month, cards)
        months_touched.append(month)
    if months_touched:
        index = _reindex(index, months_touched, data_dir, now)
    _save(index_path, index)

    # assemble active file
    d = state["diff"]
    status = "OK" if not errors else "PARTIAL"
    scan_log = list(prev.get("scan_log", []))
    scan_log.append({"ts": now, "status": status, "examined": examined,
                     "errors": errors, "new": len(d["new"]), "exited": len(d["exited"])})
    updated = {
        "active": state["active"], "last_scan": now, "diff": d,
        "scan_log": scan_log[-SCAN_LOG_KEEP:],
        "recently_exited": [
            {"contract_id": c["contract_id"], "symbol": c["symbol"], "right": c["right"],
             "strike": c["strike"], "expiration": c["expiration"], "exited_at": c["exited_at"],
             "exit_reason": c["exit_reason"], "days_qualified": c.get("days_qualified"),
             "month": c["exited_at"][:7]}
            for cards in archived.values() for c in cards
        ],
        "meta": {
            "provider": provider, "provider_status": status,
            "schema_version": SCHEMA_VERSION,
            "thresholds_version": THRESHOLDS_VERSION, "examined": examined,
            "universe": UNIVERSE, "generated_at": now, "last_successful_scan": now,
            "errors": errors,
            "archive_index": "data/archive_index.json",
            "total_archived": index.get("total_archived", 0),
            "boundary": ("Research only. Non-predictive structural screen of long calls & puts. "
                         "No ranking, no score, no conviction, no direction, no expected return, "
                         "no recommendation. This is a historical record of what happened after "
                         "contracts entered the structural filter — not a forecast of what will."),
            "thresholds": {"premium": [MIN_PREMIUM, MAX_PREMIUM], "max_cost": MAX_PREMIUM * 100,
                           "dte": [DTE_MIN, DTE_MAX], "min_oi": MIN_OPEN_INTEREST, "min_vol": MIN_VOLUME,
                           "max_bidask_pct": MAX_BIDASK_PCT_OF_MID * 100,
                           "max_theta_burden_pct_day": MAX_THETA_BURDEN_PER_DAY * 100},
        },
    }
    _save(data_path, updated)
    print(f"scan {now} [{provider}] examined={examined} active={len(updated['active'])} "
          f"new={len(d['new'])} still={len(d['still'])} exited={len(d['exited'])} "
          f"reentered={len(d['reentered'])} archived_total={index.get('total_archived',0)}")
    if not updated["active"]:
        print("NO STRUCTURALLY QUALIFIED CONTRACTS TODAY")
    return updated


# ============================== STORAGE (atomic) =========================
def _load(p):
    if not os.path.exists(p):
        return {"active": {}, "last_scan": None, "scan_log": []}
    with open(p) as f:
        return json.load(f)

def _load_index(p, now):
    if not os.path.exists(p):
        return _empty_index(now)
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return _empty_index(now)

def _save(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, p)

def _append_partition(data_dir, month, cards):
    """Append exited contracts to their month partition. Append-only: never deletes.
    De-dupes by (contract_id, first_detected) so a re-run of the same scan is idempotent."""
    path = os.path.join(data_dir, _partition_rel(month))
    if os.path.exists(path):
        with open(path) as f:
            part = json.load(f)
    else:
        part = {"schema_version": SCHEMA_VERSION, "month": month, "contracts": []}
    have = {(c.get("contract_id"), c.get("first_detected")) for c in part["contracts"]}
    for c in cards:
        key = (c.get("contract_id"), c.get("first_detected"))
        if key not in have:
            part["contracts"].append(c); have.add(key)
    _save(path, part)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="cboe", choices=["cboe", "sample"])
    ap.add_argument("--root", default=os.path.dirname(os.path.abspath(__file__)))
    args = ap.parse_args()
    run(args.provider, root=args.root)
