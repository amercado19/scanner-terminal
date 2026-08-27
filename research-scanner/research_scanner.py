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
SCHEMA_VERSION = "research-scanner.v3.2"
SCAN_LOG_KEEP = 200                     # rolling scan_log entries kept in active file

# ---- Research paper-trading policy (v3) ---------------------------------
# A SIMULATION policy, not a recommendation and not a real account. Defaults are
# written to data/research_policy.json on first run; edit + version-bump there.
# Every paper position stamps the policy_version and resolved params it entered
# under, so changing the file NEVER rewrites historical results.
DEFAULT_POLICY = {
    "policy_version": "paper-policy.v1",
    "initial_stop_pct": 30.0,          # hard stop at -30% from paper entry (mid)
    "trailing_activation_pct": 25.0,   # trailing stop only exists after +25%
    "trailing_distance_pct": 20.0,     # trailing stop = highest_mid * (1 - 20%)
    "time_stop_trading_days": 7,       # exit after 7 observed market-hours days
    "dte_stop": 14,                    # exit if DTE < 14 (usually pre-empted by DTE<21 filter)
    "entry_price": "mid",              # paper entry = midpoint at first market-hours scan
    "exit_price": "bid",               # conservative simulated exit = current bid
    "note": ("Research simulation only. Paper positions are not real trades, not a "
             "recommendation, and never the user's account. No profit is claimed."),
}

# US regular market hours, in America/New_York local time.
MARKET_OPEN_MIN = 9 * 60 + 30          # 09:30 ET
MARKET_CLOSE_MIN = 16 * 60             # 16:00 ET
SCAN_LOG_KEEP_V3 = SCAN_LOG_KEEP
# Max observed lag (seconds) for a delayed CBOE snapshot to still count as a CURRENT market-hours
# quote for ENTRY. provider_quote_ts is CBOE's top-level last_trade_time (the underlying's last
# print), which during live hours can trail the ~15-min quote delay (observed ~30-40 min on real
# data). 90 min comfortably covers a current-session snapshot for these liquid names while still
# firmly rejecting a frozen PRIOR-CLOSE quote (hours/overnight old). A delayed timestamp NEVER makes
# the workflow "off-hours" (that uses workflow execution time); this only gates whether a WAITING
# contract may ENTER on this scan.
MAX_ENTRY_LAG_SEC = 5400

def is_market_hours(now_iso):
    """True iff the scan wall-clock time falls in [09:30, 16:00) ET on a weekday.
    Paper entries and exits are evaluated ONLY when this is true. Pre-market,
    post-close, and weekend scans record research observations but take NO paper
    action (delayed quotes off-hours are just the prior close — never an entry)."""
    try:
        from datetime import datetime as _dt
        try:
            from zoneinfo import ZoneInfo
            t = _dt.fromisoformat(now_iso).astimezone(ZoneInfo("America/New_York"))
        except Exception:
            # fallback: treat the iso as UTC and apply a fixed -4 (EDT) offset
            base = _dt.fromisoformat(now_iso.replace("Z", "+00:00"))
            from datetime import timedelta
            t = base - timedelta(hours=4)
        if t.weekday() >= 5:
            return False
        mins = t.hour * 60 + t.minute
        return MARKET_OPEN_MIN <= mins < MARKET_CLOSE_MIN
    except Exception:
        return False

# The scanner MUST NOT emit any of these (enforced by a test below):
FORBIDDEN = ("chance_of_profit", "expected_return", "win_probability", "score",
             "rank", "quality_score", "conviction", "alpha", "direction",
             "recommendation", "buy", "sell")

# CBOE is a DELAYED feed (~15 min). Every observation is labelled accordingly.
CBOE_PROVIDER = "cboe"
CBOE_PROVIDER_MODE = "DELAYED"

def _lag_seconds(provider_ts, now_iso):
    """Observed data lag = ingestion time (now, UTC) − provider quote time.
    provider_ts is CBOE's last_trade_time, a naive US/Eastern stamp. Returns whole
    seconds, or None if it can't be computed (never fabricated)."""
    if not provider_ts or not now_iso:
        return None
    try:
        from datetime import datetime as _dt, timezone as _tz
        try:
            from zoneinfo import ZoneInfo
            pt = _dt.fromisoformat(provider_ts).replace(tzinfo=ZoneInfo("America/New_York"))
        except Exception:
            from datetime import timedelta
            pt = _dt.fromisoformat(provider_ts).replace(tzinfo=_tz(timedelta(hours=-4)))
        nt = _dt.fromisoformat(now_iso.replace("Z", "+00:00"))
        if nt.tzinfo is None:
            nt = nt.replace(tzinfo=_tz.utc)
        return int((nt - pt).total_seconds())
    except Exception:
        return None

HDR = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
       "Accept": "application/json", "Referer": "https://www.cboe.com/"}

# ============================== DATA MODEL ================================
class Contract:
    __slots__ = ("symbol", "right", "strike", "expiration", "underlying", "bid",
                 "ask", "mark", "iv", "delta", "theta", "open_interest",
                 "volume", "dte", "hv", "earnings_in_window", "days_to_earnings",
                 # provider/feed metadata (Phase 1: delayed-feed timestamps)
                 "provider", "provider_mode", "provider_quote_ts", "option_quote_ts")

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
        # feed metadata (Phase 1)
        "provider": getattr(c, "provider", None) or CBOE_PROVIDER,
        "provider_mode": getattr(c, "provider_mode", None) or CBOE_PROVIDER_MODE,
        "provider_quote_ts": getattr(c, "provider_quote_ts", None),
        "option_quote_ts": getattr(c, "option_quote_ts", None),
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
    """One point on a contract's timeline. Only real, scanned values — never fabricated.
    ts        = ingestion timestamp (when the workflow observed this), UTC.
    provider_quote_ts / observed_lag_sec label how delayed the underlying feed was."""
    d = res["descriptors"]
    pqt = res.get("provider_quote_ts")
    return {
        "ts": now,                                   # ingestion timestamp (workflow execution)
        "underlying": res.get("underlying"),
        "bid": res.get("bid"), "ask": res.get("ask"), "mid": res.get("mark"),
        "premium": res.get("mark"),
        "iv": d.get("iv"), "delta": d.get("delta"), "theta": d.get("theta"),
        "dte": res.get("dte"), "volume": d.get("volume"),
        "open_interest": d.get("open_interest"), "bid_ask_pct": d.get("bid_ask_pct"),
        "passed": bool(res.get("passed")),
        # feed metadata
        "provider": res.get("provider") or CBOE_PROVIDER,
        "provider_mode": res.get("provider_mode") or CBOE_PROVIDER_MODE,
        "provider_quote_ts": pqt,                    # CBOE last_trade_time (US/Eastern)
        "option_quote_ts": res.get("option_quote_ts"),
        "ingestion_ts": now,
        "observed_lag_sec": _lag_seconds(pqt, now),
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
        "research_status": "ACTIVE",
        "research_left_reason": None,
        "research_status_log": [{"ts": now, "status": "NEW"}],
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


# ============================== PAPER POSITION (v3) ====================
# A disciplined market-hours paper SIMULATION layered on top of the research
# lifecycle. Completely separate from the research record: the research contract
# keeps recording observations regardless of what the paper position does.
def _policy_params(policy):
    return {
        "initial_stop_pct": policy["initial_stop_pct"],
        "trailing_activation_pct": policy["trailing_activation_pct"],
        "trailing_distance_pct": policy["trailing_distance_pct"],
        "time_stop_trading_days": policy["time_stop_trading_days"],
        "dte_stop": policy["dte_stop"],
        "entry_price": policy.get("entry_price", "mid"),
        "exit_price": policy.get("exit_price", "bid"),
    }

def _paper_init(policy):
    """Skeleton for a not-yet-entered paper position. No entry is fabricated."""
    return {
        "status": "WAITING_FOR_ENTRY",
        "policy_version": policy["policy_version"],
        "params": _policy_params(policy),
        "entry_ts": None, "entry_bid": None, "entry_ask": None, "entry_mid": None,
        "entry_dte": None, "entry_iv": None, "entry_delta": None, "entry_theta": None,
        "entry_underlying": None,
        "current_bid": None, "current_ask": None, "current_mid": None,
        "current_underlying": None, "current_dte": None,
        "current_pct": None, "highest_pct": None, "lowest_pct": None,
        "highest_mid": None, "lowest_mid": None,
        "current_underlying_pct": None, "highest_underlying_pct": None, "lowest_underlying_pct": None,
        "highest_underlying": None, "lowest_underlying": None,
        "mfe": None, "mae": None,
        "initial_stop_level": None, "trailing_active": False, "trailing_high": None,
        "trailing_stop_level": None, "current_stop_level": None,
        "days_held": 0, "trading_days_remaining": None,
        "exit_ts": None, "exit_reason": None, "exit_price": None, "exit_note": None,
        "observations": [],       # midpoint/bid history, separate from research observations
        # explicit entry-eligibility diagnostics — a WAITING position is NEVER left unexplained
        "waiting_reason": "WAITING — NO MARKET-HOURS SCAN YET",
        "last_entry_evaluation_time": None,
        "market_session_state": None,          # REGULAR | CLOSED
        "provider_quote_timestamp": None,
        "observed_lag": None,
        "latest_qualification_state": None,    # QUALIFIES | DOES_NOT_QUALIFY | NOT_EVALUATED
    }

def _paper_enter(pp, res, now):
    """Fill the entry at the first market-hours scan. Entry price = midpoint."""
    p = pp["params"]
    mid = res.get("mark"); bid = res.get("bid"); ask = res.get("ask")
    de = res.get("descriptors", {})
    pp["status"] = "ACTIVE"
    pp["entry_ts"] = now
    pp["entry_bid"] = bid; pp["entry_ask"] = ask; pp["entry_mid"] = mid
    pp["entry_dte"] = res.get("dte"); pp["entry_iv"] = de.get("iv")
    pp["entry_delta"] = de.get("delta"); pp["entry_theta"] = de.get("theta")
    pp["entry_underlying"] = res.get("underlying")
    pp["initial_stop_level"] = round(mid * (1 - p["initial_stop_pct"] / 100), 4) if mid else None
    _paper_observe(pp, res, now)
    _paper_recompute(pp)
    return pp

def _paper_observe(pp, res, now):
    pqt = res.get("provider_quote_ts")
    pp["observations"].append({
        "ts": now, "bid": res.get("bid"), "ask": res.get("ask"), "mid": res.get("mark"),
        "underlying": res.get("underlying"), "dte": res.get("dte"),
        "trailing_active": pp.get("trailing_active", False),
        "stop_level": pp.get("current_stop_level"),
        "in_filter": bool(res.get("passed")),   # research pass/fail at this paper observation
        # feed metadata (Phase 1 delayed feed; Phase 2 worker will write REALTIME here)
        "provider": res.get("provider") or CBOE_PROVIDER,
        "provider_mode": res.get("provider_mode") or CBOE_PROVIDER_MODE,
        "provider_quote_ts": pqt, "ingestion_ts": now,
        "observed_lag_sec": _lag_seconds(pqt, now),
    })

def _paper_recompute(pp):
    obs = pp["observations"]
    mids = [o["mid"] for o in obs if o.get("mid") is not None]
    unds = [o["underlying"] for o in obs if o.get("underlying") is not None]
    e = pp.get("entry_mid"); eu = pp.get("entry_underlying")
    cur_mid = mids[-1] if mids else None
    cur_und = unds[-1] if unds else None
    pp["current_mid"] = cur_mid
    pp["current_bid"] = obs[-1]["bid"] if obs else None
    pp["current_ask"] = obs[-1]["ask"] if obs else None
    pp["current_underlying"] = cur_und
    pp["current_dte"] = obs[-1]["dte"] if obs else None
    pp["highest_mid"] = max(mids) if mids else None
    pp["lowest_mid"] = min(mids) if mids else None
    pp["highest_underlying"] = max(unds) if unds else None
    pp["lowest_underlying"] = min(unds) if unds else None
    pp["current_pct"] = _pct(cur_mid, e)
    pp["highest_pct"] = _pct(pp["highest_mid"], e)
    pp["lowest_pct"] = _pct(pp["lowest_mid"], e)
    pp["mfe"] = pp["highest_pct"]; pp["mae"] = pp["lowest_pct"]
    pp["current_underlying_pct"] = _pct(cur_und, eu)
    pp["highest_underlying_pct"] = _pct(pp["highest_underlying"], eu)
    pp["lowest_underlying_pct"] = _pct(pp["lowest_underlying"], eu)
    # distinct observed market-hours dates = trading days held
    pp["days_held"] = len({o["ts"][:10] for o in obs if o.get("ts")})
    tstop = pp["params"]["time_stop_trading_days"]
    pp["trading_days_remaining"] = max(0, tstop - pp["days_held"])
    # stop levels
    if pp.get("trailing_active"):
        pp["current_stop_level"] = pp.get("trailing_stop_level")
    else:
        pp["current_stop_level"] = pp.get("initial_stop_level")
    # feed metadata surfaced to the card + a coarse feed-health label (Phase 1: DELAYED).
    # Phase 2's real-time worker overwrites feed_mode/feed_health/feed_provider with LIVE/STALE/etc.
    last = obs[-1] if obs else {}
    pp["feed_provider"] = last.get("provider")
    pp["feed_mode"] = last.get("provider_mode")
    pp["quote_ts"] = last.get("provider_quote_ts")
    pp["last_update_ts"] = last.get("ts")
    pp["observed_lag_sec"] = last.get("observed_lag_sec")
    lag = last.get("observed_lag_sec")
    if not obs:
        pp["feed_health"] = "NO_DATA"
    elif last.get("provider_mode") == "DELAYED":
        pp["feed_health"] = "DELAYED"          # expected for CBOE; not an anomaly
    elif lag is None:
        pp["feed_health"] = "UNKNOWN"
    elif lag <= 120:
        pp["feed_health"] = "LIVE"
    elif lag <= 900:
        pp["feed_health"] = "DELAYED"
    else:
        pp["feed_health"] = "STALE"

def _paper_close(pp, reason, now, price, note):
    pp["status"] = "CLOSED"
    pp["exit_ts"] = now
    pp["exit_reason"] = reason
    pp["exit_price"] = price
    pp["exit_note"] = note

def _paper_step(pp, res, now):
    """Advance an open (ACTIVE/TRAILING) paper position by one MARKET-HOURS scan:
    append observation, update trailing, and check the exit ladder. Returns the
    exit reason if it closed this scan, else None."""
    p = pp["params"]
    _paper_observe(pp, res, now)
    _paper_recompute(pp)
    mid = pp["current_mid"]; bid = pp["current_bid"]; dte = pp["current_dte"]
    exit_bid = bid if bid is not None else mid   # conservative simulated exit

    # 1) EXPIRED
    if dte is not None and dte <= 0:
        _paper_close(pp, "EXPIRED", now, exit_bid, "contract reached expiration"); return "EXPIRED"

    if mid is not None and pp.get("entry_mid"):
        # 2) trailing activation (only after +activation%)
        if not pp["trailing_active"] and pp["current_pct"] is not None and \
           pp["current_pct"] >= p["trailing_activation_pct"]:
            pp["trailing_active"] = True
            pp["status"] = "TRAILING_ACTIVE"
            pp["trailing_high"] = pp["highest_mid"]
            pp["trailing_stop_level"] = round(pp["trailing_high"] * (1 - p["trailing_distance_pct"] / 100), 4)
            pp["current_stop_level"] = pp["trailing_stop_level"]
        # 3) trailing high only ever increases; stop never decreases
        if pp["trailing_active"]:
            if pp["trailing_high"] is None or mid > pp["trailing_high"]:
                pp["trailing_high"] = mid
            new_level = round(pp["trailing_high"] * (1 - p["trailing_distance_pct"] / 100), 4)
            pp["trailing_stop_level"] = max(new_level, pp["trailing_stop_level"] or new_level)
            pp["current_stop_level"] = pp["trailing_stop_level"]
            # 4) TRAILING STOP hit (record 'first observed', exit at current bid)
            if mid <= pp["trailing_stop_level"]:
                _paper_close(pp, "TRAILING_STOP", now, exit_bid,
                             f"stop first observed at mid {mid} (<= trailing {pp['trailing_stop_level']}); "
                             f"conservative exit at bid {exit_bid}")
                return "TRAILING_STOP"
        else:
            # 5) INITIAL STOP (pre-trailing only): -initial_stop% from entry
            if pp["initial_stop_level"] is not None and mid <= pp["initial_stop_level"]:
                _paper_close(pp, "INITIAL_STOP", now, exit_bid,
                             f"stop first observed at mid {mid} (<= initial {pp['initial_stop_level']}); "
                             f"conservative exit at bid {exit_bid}")
                return "INITIAL_STOP"

    # 6) TIME STOP
    if pp["days_held"] >= p["time_stop_trading_days"]:
        _paper_close(pp, "TIME_STOP", now, exit_bid,
                     f"reached {pp['days_held']} observed trading days"); return "TIME_STOP"
    # 7) DTE STOP (usually pre-empted by the research DTE<21 filter)
    if dte is not None and dte < p["dte_stop"]:
        _paper_close(pp, "DTE_STOP", now, exit_bid, f"DTE {dte} < {p['dte_stop']}"); return "DTE_STOP"
    return None

def _paper_never_entered(pp, now, note):
    pp["status"] = "NEVER_ENTERED"
    pp["exit_ts"] = now
    pp["exit_reason"] = "NEVER ENTERED"
    pp["exit_note"] = note

def paper_update(card, res, now, policy, market_hours):
    """Advance a card's paper position by one scan, INDEPENDENTLY of the research filter.

    Entry still requires the contract to be discovered (passing) at a market-hours scan —
    you only "buy" what the scanner would find. Once entered, the position is tracked and
    its stops evaluated on every market-hours scan REGARDLESS of whether the contract still
    passes the filter. It closes ONLY on a paper rule: initial / trailing / time / DTE stop,
    or expiration. Leaving the research filter never closes it.
    `res` is this scan's screen() result for the contract (None if not quotable this scan)."""
    pp = card.get("paper_position") or _paper_init(policy)
    card["paper_position"] = pp
    if pp["status"] in ("CLOSED", "NEVER_ENTERED"):
        return pp                                   # terminal; research history continues elsewhere
    if pp["status"] == "WAITING_FOR_ENTRY":
        return _paper_evaluate_entry(pp, res, now, market_hours)   # always records WHY (waiting_reason)
    # ACTIVE / TRAILING_ACTIVE: track + evaluate stops only on a market-hours quote
    if not market_hours or res is None:
        return pp                                   # never act on off-hours / missing prices
    _paper_step(pp, res, now)                       # tracks + stops regardless of res["passed"]
    return pp


def _paper_evaluate_entry(pp, res, now, market_hours):
    """Decide whether a WAITING paper position enters THIS scan, and ALWAYS record the reason.

    Entry rule (explicit, per the honesty contract):
      * eligibility is the WORKFLOW's actual execution time in America/New_York — a delayed quote
        timestamp NEVER makes the workflow 'off-hours';
      * it must be a regular trading session (market_hours True);
      * the contract must still QUALIFY (passing screen this scan — you only buy what the scanner finds);
      * the quote must be a CURRENT delayed snapshot (observed lag within MAX_ENTRY_LAG_SEC), never a
        frozen prior-close quote;
      * entry price = observed midpoint; no backfill, never yesterday's close.
    Otherwise the position stays WAITING with a precise waiting_reason (or terminates NEVER_ENTERED
    when it has left the research filter and can never be bought)."""
    pqt = res.get("provider_quote_ts") if res else None
    lag = _lag_seconds(pqt, now)
    passed = bool(res and res.get("passed"))
    # diagnostics recorded on EVERY evaluation, entered or not
    pp["last_entry_evaluation_time"] = now
    pp["market_session_state"] = "REGULAR" if market_hours else "CLOSED"
    pp["provider_quote_timestamp"] = pqt
    pp["observed_lag"] = lag
    pp["latest_qualification_state"] = ("QUALIFIES" if passed
                                        else ("DOES_NOT_QUALIFY" if res is not None else "NOT_EVALUATED"))
    if not market_hours:
        pp["waiting_reason"] = "WAITING — NO MARKET-HOURS SCAN YET"
        return pp
    if res is None:
        pp["waiting_reason"] = "WAITING — PROVIDER FAILED"
        return pp
    if not passed:
        # left the research filter before ever entering — it can never be 'bought'. Terminal.
        pp["waiting_reason"] = "CONTRACT NO LONGER QUALIFIES"
        _paper_never_entered(pp, now, "CONTRACT NO LONGER QUALIFIES — left the research filter before a market-hours entry")
        return pp
    # qualifies in a regular session: require a CURRENT delayed snapshot, never a frozen prior-close.
    # (A missing provider timestamp — synthetic fixtures — is not treated as stale.)
    stale = pqt is not None and (lag is None or lag < -300 or lag > MAX_ENTRY_LAG_SEC)
    if stale:
        pp["waiting_reason"] = "WAITING — PROVIDER QUOTE STALE"
        return pp
    _paper_enter(pp, res, now)                       # ENTER at the observed midpoint
    pp["waiting_reason"] = None
    pp["last_entry_evaluation_time"] = now
    pp["market_session_state"] = "REGULAR"
    pp["provider_quote_timestamp"] = pqt
    pp["observed_lag"] = lag
    pp["latest_qualification_state"] = "QUALIFIES"
    return pp


# ============================== MIGRATION (v1 -> v2/v3) =================
def _migrate_card(card, now):
    """Bring a legacy v1 active card up to the v2 historical schema, in place,
    without inventing data. Seeds observations from the legacy history when needed."""
    if "observations" in card and card.get("current_status") in ("NEW", "ACTIVE", "EXITED",
                                                                  "LEFT_FILTER", "RETURNED_TO_FILTER", "EXPIRED"):
        # already v2/v3-shaped; ensure derived metrics + v3.1 research-status fields exist
        card.setdefault("last_seen", card.get("last_updated") or now)
        if "research_status" not in card:
            cs = card.get("current_status")
            card["research_status"] = "ACTIVE" if cs in ("NEW", "ACTIVE") else (cs or "ACTIVE")
            card.setdefault("research_left_reason", None)
            card.setdefault("research_status_log", [{"ts": card.get("first_detected") or now, "status": "ACTIVE"}])
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
    card.setdefault("research_status", "ACTIVE")
    card.setdefault("research_left_reason", None)
    card.setdefault("research_status_log", [{"ts": card.get("first_detected") or now, "status": "ACTIVE"}])
    card.setdefault("entry_reason", "passed all frozen structural filters")
    return _recompute(card)


# ============================== ARCHIVE INDEX / PARTITIONS ==============
def _empty_index(now):
    return {"schema_version": SCHEMA_VERSION, "updated_at": now, "total_archived": 0,
            "tickers": [], "seen_contract_ids": [], "partitions": [], "stats": None,
            "paper_stats": None}

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

def paper_stats(contracts):
    """Descriptive-only summary of the SIMULATED paper positions on a set of archived
    contracts. Counts, averages, and distributions — never a ranking, a win rate, an
    edge claim, or a forecast. Research return and paper return are reported side by
    side but never merged."""
    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None
    def bucket(days):
        if days is None: return "unknown"
        return "0-1" if days <= 1 else "2-3" if days <= 3 else "4-5" if days <= 5 else "6-7" if days <= 7 else "8+"
    pps = [c.get("paper_position") or {} for c in contracts]
    entered = [p for p in pps if p.get("entry_ts")]
    closed = [p for p in entered if p.get("status") == "CLOSED"]
    never = [p for p in pps if p.get("status") == "NEVER_ENTERED"]
    exit_dist, hold_dist, dte_dist = {}, {}, {}
    outlived = 0                       # positions still open past a research filter-exit
    post_mfe, post_mae = [], []        # paper excursion AFTER the contract left the filter
    for p in closed:
        r = p.get("exit_reason") or "unknown"; exit_dist[r] = exit_dist.get(r, 0) + 1
        hb = bucket(p.get("days_held")); hold_dist[hb] = hold_dist.get(hb, 0) + 1
        d = p.get("current_dte")
        db = "unknown" if d is None else ("<14" if d < 14 else "14-20" if d < 21 else "21+")
        dte_dist[db] = dte_dist.get(db, 0) + 1
        e = p.get("entry_mid")
        postobs = [o for o in p.get("observations", []) if o.get("in_filter") is False and o.get("mid") is not None]
        if postobs and e:
            outlived += 1
            pcts = [(o["mid"] - e) / e * 100 for o in postobs]
            post_mfe.append(round(max(pcts), 2)); post_mae.append(round(min(pcts), 2))
    research_returns = [c.get("lifetime", {}).get("option_return_pct") for c in contracts]
    return {
        "contracts_entered": len(entered),
        "contracts_never_entered": len(never),
        "avg_days_held": avg([p.get("days_held") for p in closed]),
        "avg_paper_return_pct": avg([p.get("current_pct") for p in closed]),
        "avg_research_return_pct": avg(research_returns),
        "avg_mfe_pct": avg([p.get("mfe") for p in closed]),
        "avg_mae_pct": avg([p.get("mae") for p in closed]),
        "closed_after_leaving_filter": outlived,
        "avg_mfe_after_filter_left_pct": avg(post_mfe),
        "avg_mae_after_filter_left_pct": avg(post_mae),
        "exit_reason_distribution": dict(sorted(exit_dist.items())),
        "hold_time_distribution": dict(sorted(hold_dist.items())),
        "dte_at_exit_distribution": dict(sorted(dte_dist.items())),
        "trailing_stops": exit_dist.get("TRAILING_STOP", 0),
        "initial_stops": exit_dist.get("INITIAL_STOP", 0),
        "time_stops": exit_dist.get("TIME_STOP", 0),
        "dte_stops": exit_dist.get("DTE_STOP", 0),
        "filter_exits": exit_dist.get("FILTER_EXIT", 0),
        "expired": exit_dist.get("EXPIRED", 0),
        "note": "Simulation only. Descriptive of past paper trades; not indicative of future results, not a recommendation, not a real account.",
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

def _all_archived_contracts(data_dir, index):
    out = []
    for p in index.get("partitions", []):
        try:
            with open(os.path.join(data_dir, p["path"])) as f:
                out.extend(json.load(f).get("contracts", []))
        except Exception:
            pass
    return out


# ============================== SCAN UPDATE ============================
def _advance_research(card, res, now):
    """Advance the RESEARCH lifecycle by one scan — independent of the paper position.
    Records a research observation every scan (whether or not the contract still passes)
    and tracks whether today's scanner would still discover it. NEVER closes a paper
    position. research_status transitions: ACTIVE <-> LEFT_FILTER (RETURNED_TO_FILTER
    logged on the way back), and EXPIRED (terminal, set elsewhere)."""
    passed = bool(res["passed"])
    card["observations"].append(_observation(res, now))
    card["current_premium"] = res["mark"]
    card["premium_change"] = round((res["mark"] or 0) - (card.get("entry_premium") or 0), 2)
    card["current_underlying"] = res["underlying"]
    card["current_iv"] = res["descriptors"].get("iv")
    card["dte"] = res["dte"]
    card["descriptors"] = res["descriptors"]
    card["checks"] = res["checks"]
    card["last_seen"] = now
    card["last_updated"] = now
    prev_status = card.get("research_status", "ACTIVE")
    log = card.setdefault("research_status_log", [])
    if passed:
        if prev_status == "LEFT_FILTER":
            log.append({"ts": now, "status": "RETURNED_TO_FILTER"})
            card["history"].append({"ts": now, "event": "returned", "premium": res["mark"],
                                    "underlying": res["underlying"], "dte": res["dte"]})
        else:
            card["history"].append({"ts": now, "event": "still", "premium": res["mark"],
                                    "underlying": res["underlying"], "iv": res["descriptors"].get("iv"), "dte": res["dte"]})
        card["research_status"] = "ACTIVE"
        card["research_left_reason"] = None
        card["current_status"] = "ACTIVE"
    else:
        reason = exit_reason(res)
        if prev_status != "LEFT_FILTER":
            log.append({"ts": now, "status": "LEFT_FILTER", "reason": reason})
        card["research_status"] = "LEFT_FILTER"
        card["research_left_reason"] = reason
        card["current_status"] = "LEFT_FILTER"
        card["history"].append({"ts": now, "event": "left_filter", "reason": reason,
                                "premium": res["mark"], "underlying": res["underlying"], "dte": res["dte"]})
    _recompute(card)
    return card

def update_watchlist(prev, discovery_results, now, track_results=None, ok_symbols=None,
                     seen_ids=None, policy=None, market_hours=None):
    """Advance both lifecycles by one scan.

    Two independent loops:
      * Loop 1 (Discovery)  — `discovery_results`: in-window screen() dicts, used ONLY to
        find NEW qualifying contracts to start tracking.
      * Loop 2 (Tracker)    — `track_results`: {contract_id: screen() dict} for EVERY
        existing active record we could still quote this scan (ANY DTE). Advances each
        record's research lifecycle AND its paper position independently.

    A record leaves the active file (archives) ONLY when its paper position is terminal
    (CLOSED / NEVER_ENTERED) AND its research lifecycle is out of the filter
    (LEFT_FILTER / EXPIRED). A contract that has left the filter but still holds an open
    paper position keeps being tracked; a closed paper position on a still-qualifying
    contract keeps recording research observations. Both histories archive together.
    A symbol that fails to fetch holds its records with NO fabricated observation.
    """
    policy = policy or DEFAULT_POLICY
    if market_hours is None:
        market_hours = is_market_hours(now)
    if track_results is None:
        # convenience default: every screened contract is available to the tracker
        track_results = {r["contract_id"]: r for r in discovery_results}
    active = {cid: _migrate_card(dict(c), now) for cid, c in prev.get("active", {}).items()}
    disc_by_id = {r["contract_id"]: r for r in discovery_results}
    diff = {"new": [], "still": [], "exited": [], "reentered": [], "left_filter": [], "returned": []}
    archived = {}
    seen_ids = set(seen_ids or [])
    ok = None if ok_symbols is None else set(ok_symbols)

    def finalize_and_archive(card, research_reason):
        card["exit_reason"] = research_reason
        card["exited_at"] = now
        card["last_updated"] = now
        card["lifetime"] = _lifetime(card)
        archived.setdefault(now[:7], []).append(card)
        diff["exited"].append(card["contract_id"])

    for cid, card in list(active.items()):
        tr = track_results.get(cid)
        sym = card.get("symbol")
        if tr is not None:
            prev_rs = card.get("research_status", "ACTIVE")
            _advance_research(card, tr, now)
            if card["research_status"] == "ACTIVE":
                diff["still"].append(cid)
                if prev_rs == "LEFT_FILTER":
                    diff["returned"].append(cid)
            elif prev_rs != "LEFT_FILTER":
                diff["left_filter"].append(cid)
            # paper position advances independently (entry needs a passing quote; tracking does not)
            paper_update(card, tr, now, policy, market_hours)
        elif ok is not None and sym not in ok:
            card["last_updated"] = now                     # data gap — hold, no fabrication
        else:
            # symbol fetched but this contract is gone from the chain
            try:
                expired = date.fromisoformat(card.get("expiration")) < date.fromisoformat(now[:10])
            except Exception:
                expired = False
            if expired:
                card["research_status"] = "EXPIRED"
                card["research_left_reason"] = "expired"
                card["current_status"] = "EXPIRED"
                card.setdefault("research_status_log", []).append({"ts": now, "status": "EXPIRED"})
                pp = card.get("paper_position")
                if pp and market_hours:
                    if pp["status"] in ("ACTIVE", "TRAILING_ACTIVE"):
                        _paper_close(pp, "EXPIRED", now, pp.get("current_bid") or pp.get("current_mid"),
                                     "contract expired / left the chain")
                    elif pp["status"] == "WAITING_FOR_ENTRY":
                        _paper_never_entered(pp, now, "expired before a market-hours entry")
            else:
                card["last_updated"] = now                 # transient chain miss — hold

        pp = card.get("paper_position") or {}
        paper_terminal = pp.get("status") in ("CLOSED", "NEVER_ENTERED")
        research_out = card.get("research_status") in ("LEFT_FILTER", "EXPIRED")
        if paper_terminal and research_out:
            finalize_and_archive(card, card.get("research_left_reason") or "left filter")
            del active[cid]

    # Loop 1 — discovery of NEW qualifying contracts not already tracked
    for cid, r in disc_by_id.items():
        if r["passed"] and cid not in active:
            reentered = cid in seen_ids
            card = _new_card(r, now, reentered=reentered)
            paper_update(card, r, now, policy, market_hours)
            active[cid] = card
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
    provider_quote_ts = data.get("last_trade_time")   # CBOE snapshot reference time (US/Eastern)
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
        # Return the full TRACKING range (0..DTE_MAX+3): discovery uses the in-window
        # subset, while the paper-position tracker (Loop 2) needs contracts that have
        # already dropped below the discovery window (down to expiry).
        if dte is None or not (0 <= dte <= DTE_MAX + 3):
            continue
        bid, ask = o.get("bid"), o.get("ask")
        mark = ((bid + ask) / 2) if (bid not in (None, 0) or ask not in (None, 0)) else None
        out.append(Contract(symbol=symbol, right=right, strike=strike, expiration=exp,
                            underlying=underlying, bid=bid, ask=ask, mark=mark, iv=o.get("iv"),
                            delta=o.get("delta"), theta=o.get("theta"),
                            open_interest=o.get("open_interest"), volume=o.get("volume"),
                            dte=dte, hv=hv, earnings_in_window=None, days_to_earnings=None,
                            provider=CBOE_PROVIDER, provider_mode=CBOE_PROVIDER_MODE,
                            provider_quote_ts=provider_quote_ts, option_quote_ts=o.get("last_trade_time")))
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
                            earnings_in_window=None, days_to_earnings=None,
                            provider=CBOE_PROVIDER, provider_mode=CBOE_PROVIDER_MODE,
                            provider_quote_ts="2026-08-26T16:00:00", option_quote_ts="2026-08-26T14:28:13"))
    return out


# ============================== RUN ======================================
def run(provider, today=None, now=None, root="."):
    today = today or date.today()
    now = now or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    data_dir = os.path.join(root, "data")
    data_path = os.path.join(data_dir, "research_watchlist.json")
    index_path = os.path.join(data_dir, "archive_index.json")
    policy_path = os.path.join(data_dir, "research_policy.json")
    policy = _load_policy(policy_path)
    market_hours = is_market_hours(now)

    cand = {"cboe": cboe_candidates, "sample": sample_candidates}[provider]
    # Loop 1 fetch: one chain pull per symbol yields BOTH the discovery candidates
    # (in the DTE window) and a full contract map used by the Loop 2 tracker.
    discovery, examined, errors, ok_symbols = [], 0, [], set()
    raw = {}   # contract_id -> Contract for EVERY fetched option (any DTE) — the tracking feed
    quote_tss = []   # provider quote timestamps seen this scan (for scan-level lag)
    for sym in UNIVERSE:
        try:
            for c in cand(sym, today):
                raw[c.contract_id] = c
                if getattr(c, "provider_quote_ts", None):
                    quote_tss.append(c.provider_quote_ts)
                if DTE_MIN - 3 <= (c.dte if c.dte is not None else -1) <= DTE_MAX + 3:
                    discovery.append(screen(c)); examined += 1
            ok_symbols.add(sym)
            time.sleep(0.4)  # be gentle with CBOE
        except Exception as e:
            errors.append(f"{sym}: {e}")
            print(f"[warn] {sym}: {e}", file=sys.stderr)
    scan_quote_ts = max(quote_tss) if quote_tss else None       # freshest provider stamp this scan
    scan_lag_sec = _lag_seconds(scan_quote_ts, now)

    prev = _load(data_path)
    index = _load_index(index_path, now)

    # Loop 2 feed: a fresh screen() for every EXISTING active record we can still quote
    # this scan, at ANY DTE — this is what keeps paper positions tracked after they leave
    # the discovery window / filter.
    track_results = {cid: screen(raw[cid]) for cid in prev.get("active", {}).keys() if cid in raw}

    # FAILURE RULE: if EVERY symbol failed to fetch, do NOT advance the record and do NOT
    # overwrite the last valid state. Log the failure to a SEPARATE failed_scans list.
    if provider == "cboe" and errors and examined == 0:
        prev.setdefault("meta", {})
        prev["meta"]["provider_status"] = "OFFLINE"
        prev["meta"]["last_error_at"] = now
        prev["meta"]["errors"] = errors
        prev.setdefault("scan_log", [])
        prev["scan_log"].append({"ts": now, "status": "OFFLINE", "examined": 0, "errors": errors,
                                 "market_hours": market_hours})
        prev["scan_log"] = prev["scan_log"][-SCAN_LOG_KEEP:]
        prev.setdefault("failed_scans", [])
        prev["failed_scans"].append({"ts": now, "status": "OFFLINE", "errors": errors,
                                     "market_hours": market_hours})
        prev["failed_scans"] = prev["failed_scans"][-SCAN_LOG_KEEP:]
        # fingerprint over PRESERVED state + OFFLINE status: the FIRST offline flips the
        # status → commit (operator sees the outage); repeated offline scans keep the same
        # fingerprint → the workflow skips them. Last valid positions are never overwritten.
        fp = _state_fingerprint(prev, "OFFLINE")
        prev.setdefault("meta", {})["state_fingerprint"] = fp
        _save(data_path, prev)
        _write_fingerprint(data_dir, fp)
        print("PROVIDER OFFLINE — kept previous watchlist (last good scan: "
              f"{prev.get('last_scan')})", file=sys.stderr)
        return prev

    seen_ids = set(index.get("seen_contract_ids", []))
    state, archived = update_watchlist(prev, discovery, now, track_results=track_results,
                                       ok_symbols=ok_symbols, seen_ids=seen_ids,
                                       policy=policy, market_hours=market_hours)

    # write archive partitions (append-only), then reindex
    months_touched = []
    for month, cards in archived.items():
        _append_partition(data_dir, month, cards)
        months_touched.append(month)
    if months_touched:
        index = _reindex(index, months_touched, data_dir, now)
    _save(index_path, index)

    # Paper simulations complete when the paper position closes — which can happen
    # while the research contract is still active. So paper_stats spans BOTH the
    # closed positions on active cards and every archived contract.
    _union = list(state["active"].values()) + _all_archived_contracts(data_dir, index)
    _paper_terminal = [c for c in _union
                       if (c.get("paper_position") or {}).get("status") in ("CLOSED", "NEVER_ENTERED")]
    meta_paper_stats = paper_stats(_paper_terminal)

    # assemble active file
    d = state["diff"]
    status = "OK" if not errors else "PARTIAL"
    scan_log = list(prev.get("scan_log", []))
    scan_log.append({"ts": now, "status": status, "examined": examined,
                     "errors": errors, "new": len(d["new"]), "exited": len(d["exited"]),
                     "left_filter": len(d.get("left_filter", [])), "returned": len(d.get("returned", [])),
                     "market_hours": market_hours, "tracked": len(track_results),
                     "provider_quote_ts": scan_quote_ts, "observed_lag_sec": scan_lag_sec})
    failed_scans = list(prev.get("failed_scans", []))[-SCAN_LOG_KEEP:]   # preserved separately
    # live tallies (descriptive, over currently-active cards)
    _cards = list(state["active"].values())
    _pp = [c.get("paper_position", {}) for c in _cards]
    paper_live = {
        "waiting": sum(1 for p in _pp if p.get("status") == "WAITING_FOR_ENTRY"),
        "active": sum(1 for p in _pp if p.get("status") == "ACTIVE"),
        "trailing": sum(1 for p in _pp if p.get("status") == "TRAILING_ACTIVE"),
        # open paper positions still being tracked AFTER their contract left the research filter
        "tracked_past_filter": sum(1 for c in _cards
                                   if c.get("research_status") in ("LEFT_FILTER", "EXPIRED")
                                   and (c.get("paper_position") or {}).get("status") in ("ACTIVE", "TRAILING_ACTIVE")),
    }
    research_live = {
        "in_filter": sum(1 for c in _cards if c.get("research_status") == "ACTIVE"),
        "left_filter": sum(1 for c in _cards if c.get("research_status") == "LEFT_FILTER"),
    }
    # counts of open paper positions by feed mode/health (Phase 1: all DELAYED via CBOE;
    # Phase 2's real-time worker will move some to REALTIME and flag STALE/DISCONNECTED)
    _open = [p for p in _pp if p.get("status") in ("ACTIVE", "TRAILING_ACTIVE")]
    feed = {
        "discovery_provider": CBOE_PROVIDER, "discovery_mode": CBOE_PROVIDER_MODE,
        "provider_quote_ts": scan_quote_ts, "workflow_ts": now, "observed_lag_sec": scan_lag_sec,
        "delayed": True,
        # Phase 2 real-time tracker — not operational until a provider credential + worker exist
        "active_position_provider": None, "active_position_status": "NOT_CONFIGURED",
        "heartbeat_ts": None, "last_realtime_update": None, "last_delayed_update": now,
        "fallback_active": False,
        "open_paper_positions": len(_open),
        "receiving_realtime": 0,
        "on_delayed_fallback": len(_open),      # Phase 1: every open position is on delayed data
        "stale_or_disconnected": 0,
    }
    updated = {
        "active": state["active"], "last_scan": now, "diff": d,
        "scan_log": scan_log[-SCAN_LOG_KEEP:],
        "failed_scans": failed_scans,
        "feed": feed,
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
            "policy_version": policy.get("policy_version"),
            "policy": _policy_params(policy),
            "market_hours": market_hours,
            "paper_live": paper_live,
            "research_live": research_live,
            "feed": feed,
            "paper_stats": meta_paper_stats,
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
    # meaningful-change fingerprint (timestamp-free) → sidecar the workflow diffs to decide
    # whether this scan is worth a commit (requirement: no identical-state Pages churn).
    fp = _state_fingerprint(updated, status)
    updated["meta"]["state_fingerprint"] = fp
    _save(data_path, updated)
    _write_fingerprint(data_dir, fp)
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

def _load_policy(p):
    """Load the versioned paper-trading policy; write defaults on first run.
    Editing this file and bumping policy_version only affects NEW positions —
    existing paper positions keep the params they stamped at entry."""
    if os.path.exists(p):
        try:
            with open(p) as f:
                pol = json.load(f)
            for k, v in DEFAULT_POLICY.items():
                pol.setdefault(k, v)      # tolerate partial files without overwriting
            return pol
        except Exception:
            pass
    _save(p, DEFAULT_POLICY)
    return dict(DEFAULT_POLICY)

def _save(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, p)

def _round(x, d=4):
    try:
        return round(float(x), d)
    except Exception:
        return None

def _state_fingerprint(watch, provider_status):
    """Stable hash of MEANINGFUL research/paper state — deliberately EXCLUDES pure
    timestamps, observed lag, heartbeat, and the scan log. Two scans that differ only by
    'when they ran' (e.g. weekend/after-hours scans where CBOE data is static) produce the
    SAME fingerprint, so the workflow can skip an identical-state commit and spare GitHub
    Pages a needless rebuild. A price move, a new/exited contract, a stop/trailing change,
    an archive, or a provider-status transition all CHANGE the fingerprint and DO commit."""
    import hashlib
    parts = [f"status={provider_status}",
             f"archived={ (watch.get('meta') or {}).get('total_archived', 0) }"]
    d = watch.get("diff") or {}
    parts.append("new=" + ",".join(sorted(d.get("new", []))))
    parts.append("exited=" + ",".join(sorted(d.get("exited", []))))
    for cid in sorted((watch.get("active") or {}).keys()):
        c = watch["active"][cid]
        pp = c.get("paper_position") or {}
        parts.append("|".join(str(v) for v in [
            cid, c.get("research_status"),
            _round(c.get("current_premium")), _round(c.get("current_underlying")),
            pp.get("status"), pp.get("exit_reason"),
            _round(pp.get("current_mid")), _round(pp.get("current_stop_level")),
            bool(pp.get("trailing_active")),
        ]))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

def _write_fingerprint(data_dir, fp):
    """Sidecar the workflow diffs to decide whether to commit. Kept tiny and timestamp-free."""
    with open(os.path.join(data_dir, "state_fingerprint.txt"), "w") as f:
        f.write(fp + "\n")

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
