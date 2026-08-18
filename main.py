#!/usr/bin/env python3
"""
SPY 0DTE Options Terminal - Background Worker
=============================================

Pipeline (runs every LOOP_SECONDS):
  1. Fetch SPY intraday 5m bars (yfinance; DELAYED ~15m - see warnings in payload).
  2. Compute intraday VWAP and 20-period / 2-std Bollinger Bands.
  3. Fetch 0DTE GEX / call wall / put wall from FlashAlpha (CACHED - free tier = 5 req/day).
  4. Deterministic rule engine decides bias + option action + confluence score.
     -> This is the TRADE TRIGGER. It is reproducible and backtestable.
  5. Claude writes a *labeled narrative only*. It never overrides the rule action.
  6. Upsert one document into Mongo (_id = spy_live_data).

Nothing here is investment advice. The rule engine is a transparent heuristic,
NOT a validated edge. Backtest it before trading real money.
"""

import os
import sys
import json
import time
import math
import datetime as dt
from typing import Optional

import requests

# Load .env robustly (handles &, ?, special chars that break shell `source`).
# Does NOT override vars already set in the environment, so inline overrides
# like `DATA_SOURCE=yfinance python main.py` still win.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ----------------------------------------------------------------------------
# Config (all via environment / .env)
# ----------------------------------------------------------------------------
def _get(name: str, default: Optional[str] = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        print(f"[FATAL] missing required env var: {name}", file=sys.stderr)
        sys.exit(1)
    return val

SYMBOL              = _get("SYMBOL", "SPY")
LOOP_SECONDS        = int(_get("LOOP_SECONDS", "120"))

# Data source for price bars: "alpaca" (real-time IEX, deployable) or "yfinance" (delayed).
DATA_SOURCE         = _get("DATA_SOURCE", "alpaca").lower()
ALPACA_KEY_ID       = _get("ALPACA_KEY_ID", "")
ALPACA_SECRET_KEY   = _get("ALPACA_SECRET_KEY", "")
ALPACA_DATA_URL     = _get("ALPACA_DATA_URL", "https://data.alpaca.markets")
ALPACA_FEED         = _get("ALPACA_FEED", "iex")   # free tier = iex
# GEX refresh is throttled independently of the price loop to respect the
# FlashAlpha free tier (5 requests/day). 4h => at most ~2-3 calls/day.
GEX_REFRESH_SECONDS = int(_get("GEX_REFRESH_SECONDS", "14400"))

FLASHALPHA_API_KEY  = _get("FLASHALPHA_API_KEY", "")
FLASHALPHA_BASE     = _get("FLASHALPHA_BASE", "https://lab.flashalpha.com")

ANTHROPIC_API_KEY   = _get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL        = _get("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")
ENABLE_LLM          = _get("ENABLE_LLM_NARRATIVE", "true").lower() == "true"

MONGODB_URI         = _get("MONGODB_URI", "")
# Preferred: give the RAW password here and let the code URL-encode it.
# Avoids "must be escaped (RFC 3986)" errors when the password has @ ! # / etc.
MONGODB_USER        = _get("MONGODB_USER", "andresmercado1919_db_user")
MONGODB_PASSWORD    = _get("MONGODB_PASSWORD", "")
MONGODB_CLUSTER_HOST= _get("MONGODB_CLUSTER_HOST", "cluster0.rku8nto.mongodb.net")
DB_NAME             = _get("DB_NAME", "spy_terminal_db")
COLLECTION_NAME     = _get("COLLECTION_NAME", "spy_payloads")
DOC_ID              = _get("DOC_ID", "spy_live_data")

# Rule-engine tunables (documented, backtestable)
BUY_THRESHOLD       = float(_get("BUY_THRESHOLD", "60"))   # confluence >= this to act
BB_PERIOD           = int(_get("BB_PERIOD", "20"))
BB_STD              = float(_get("BB_STD", "2.0"))

RUN_ONCE            = _get("RUN_ONCE", "false").lower() == "true"

# ----------------------------------------------------------------------------
# Data fetch
# ----------------------------------------------------------------------------
def fetch_spy_bars(symbol: str):
    """Dispatch to the configured data source. Returns a DataFrame with
    Open/High/Low/Close/Volume columns for today's 5-minute bars."""
    if DATA_SOURCE == "alpaca":
        return fetch_spy_bars_alpaca(symbol)
    return fetch_spy_bars_yfinance(symbol)


def fetch_spy_bars_alpaca(symbol: str):
    """Real-time-ish 5m bars from Alpaca Market Data v2 (free IEX feed).
    Docs: https://docs.alpaca.markets/reference/stockbars"""
    import pandas as pd
    if not (ALPACA_KEY_ID and ALPACA_SECRET_KEY):
        raise RuntimeError("DATA_SOURCE=alpaca but ALPACA_KEY_ID/ALPACA_SECRET_KEY are unset")
    start = dt.datetime.utcnow().strftime("%Y-%m-%dT00:00:00Z")  # start of UTC day => today's session
    url = f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/bars"
    params = {"timeframe": "5Min", "start": start, "limit": 10000,
              "adjustment": "raw", "feed": ALPACA_FEED, "sort": "asc"}
    headers = {"APCA-API-KEY-ID": ALPACA_KEY_ID, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
    r = requests.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    bars = (r.json() or {}).get("bars") or []
    if not bars:
        raise RuntimeError("Alpaca returned no bars (market closed or no IEX prints yet today)")
    df = pd.DataFrame([{
        "Open": b["o"], "High": b["h"], "Low": b["l"], "Close": b["c"], "Volume": float(b["v"]),
    } for b in bars])
    return df.dropna()


def fetch_spy_bars_yfinance(symbol: str):
    """Return a DataFrame of today's 5m bars. yfinance is DELAYED and rate-limited;
    this function is intentionally isolated so it can be swapped for a real feed."""
    import yfinance as yf
    df = yf.download(
        tickers=symbol, period="1d", interval="5m",
        auto_adjust=False, prepost=False, progress=False, threads=False,
    )
    if df is None or df.empty:
        # fall back to 2d so we still have data pre-market / thin sessions
        df = yf.download(symbol, period="2d", interval="5m",
                         auto_adjust=False, progress=False, threads=False)
    # yfinance may return a multiindex column set for a single ticker
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    return df.dropna()


def compute_vwap(df) -> float:
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    vol = df["Volume"].replace(0, 1e-9)
    return float((tp * vol).cumsum().iloc[-1] / vol.cumsum().iloc[-1])


def compute_bollinger(df, period: int, n_std: float) -> dict:
    close = df["Close"]
    if len(close) < period:
        period = max(2, len(close))
    mid = close.rolling(period).mean().iloc[-1]
    sd = close.rolling(period).std(ddof=0).iloc[-1]
    upper = mid + n_std * sd
    lower = mid - n_std * sd
    last = float(close.iloc[-1])
    width = (upper - lower)
    pct_b = (last - lower) / width if width else 0.5
    if last >= upper:
        status = "ABOVE_UPPER"
    elif last <= lower:
        status = "BELOW_LOWER"
    elif pct_b >= 0.8:
        status = "NEAR_UPPER"
    elif pct_b <= 0.2:
        status = "NEAR_LOWER"
    else:
        status = "MID_RANGE"
    return {
        "upper": round(float(upper), 2),
        "mid": round(float(mid), 2),
        "lower": round(float(lower), 2),
        "percent_b": round(float(pct_b), 3),
        "bandwidth": round(float(width), 2),
        "status": status,
    }


# GEX cache so we don't burn the 5/day free tier on every loop
_GEX_CACHE = {"data": None, "ts": 0.0}

def fetch_gex(symbol: str) -> dict:
    """0DTE GEX via FlashAlpha, cached for GEX_REFRESH_SECONDS."""
    now = time.time()
    cached = _GEX_CACHE["data"]
    if cached is not None and (now - _GEX_CACHE["ts"]) < GEX_REFRESH_SECONDS:
        out = dict(cached)
        out["stale"] = True
        return out

    if not FLASHALPHA_API_KEY:
        return {"available": False, "reason": "no FLASHALPHA_API_KEY", "stale": False}

    url = f"{FLASHALPHA_BASE}/v1/exposure/zero-dte/{symbol}"
    try:
        r = requests.get(url, headers={"X-Api-Key": FLASHALPHA_API_KEY}, timeout=20)
        if r.status_code == 429:
            # rate limited: keep whatever we had
            if cached:
                out = dict(cached); out["stale"] = True; out["reason"] = "rate_limited"; return out
            return {"available": False, "reason": "rate_limited(429)", "stale": False}
        r.raise_for_status()
        raw = r.json()
        parsed = _parse_gex(raw)
        parsed["available"] = True
        parsed["as_of"] = dt.datetime.utcnow().isoformat() + "Z"
        parsed["stale"] = False
        _GEX_CACHE["data"] = parsed
        _GEX_CACHE["ts"] = now
        return parsed
    except Exception as e:
        if cached:
            out = dict(cached); out["stale"] = True; out["reason"] = f"error:{e}"; return out
        return {"available": False, "reason": f"error:{e}", "stale": False}


def _wall_strike(obj):
    """FlashAlpha walls may be a number or an object {strike, gex}. Normalize."""
    if obj is None:
        return None
    if isinstance(obj, (int, float)):
        return float(obj)
    if isinstance(obj, dict):
        for k in ("strike", "price", "level"):
            if k in obj and obj[k] is not None:
                return float(obj[k])
    return None

def _parse_gex(raw: dict) -> dict:
    net = raw.get("net_gex")
    label = raw.get("net_gex_label")
    if not label and isinstance(net, (int, float)):
        label = "positive" if net >= 0 else "negative"
    return {
        "net_gex": net,
        "regime": (label or "unknown"),
        "gamma_flip": _wall_strike(raw.get("gamma_flip")),
        "call_wall": _wall_strike(raw.get("call_wall")),
        "put_wall": _wall_strike(raw.get("put_wall")),
    }


# ----------------------------------------------------------------------------
# Deterministic rule engine  ==  THE TRADE TRIGGER (reproducible, backtestable)
# ----------------------------------------------------------------------------
def compute_rule_signal(price: float, vwap: float, bb: dict, gex: dict) -> dict:
    """
    Weighted vote in [-100, +100]. Positive => bullish, negative => bearish.
    Confluence score = abs(vote). Action fires only when confluence >= BUY_THRESHOLD.

    Regime awareness: in POSITIVE gamma, dealers dampen moves -> fade band extremes.
    In NEGATIVE gamma, dealers amplify moves -> follow breakouts.
    """
    factors = []
    vote = 0.0

    regime = (gex.get("regime") or "unknown").lower()
    mean_reverting = regime == "positive"      # fade extremes
    trending = regime == "negative"            # follow momentum

    # 1) VWAP location (weight 30)
    if vwap:
        dev = (price - vwap) / vwap * 100.0
        w = max(-1.0, min(1.0, dev / 0.3))     # +-0.3% saturates
        vote += 30 * w
        factors.append({
            "name": "VWAP",
            "detail": f"price {'above' if dev>=0 else 'below'} VWAP by {abs(dev):.2f}%",
            "contribution": round(30 * w, 1),
        })

    # 2) Bollinger %B, interpreted by regime (weight 35)
    pb = bb.get("percent_b", 0.5)
    if mean_reverting:
        # near upper -> fade down (bearish); near lower -> fade up (bullish)
        w = (0.5 - pb) * 2.0                    # pb=0 ->+1, pb=1 ->-1
        note = "mean-revert (positive gamma): fade band extreme"
    elif trending:
        # breakout continuation
        w = (pb - 0.5) * 2.0
        note = "momentum (negative gamma): follow breakout"
    else:
        w = (0.5 - pb) * 1.0
        note = "unknown gamma regime: mild mean-revert bias"
    w = max(-1.0, min(1.0, w))
    vote += 35 * w
    factors.append({
        "name": "Bollinger %B",
        "detail": f"%B={pb:.2f}, {note}",
        "contribution": round(35 * w, 1),
    })

    # 3) Gamma flip location (weight 20)
    flip = gex.get("gamma_flip")
    if flip:
        above = price > flip
        w = 1.0 if above else -1.0
        vote += 20 * w
        factors.append({
            "name": "Gamma flip",
            "detail": f"price {'above' if above else 'below'} flip {flip}",
            "contribution": round(20 * w, 1),
        })

    # 4) Wall proximity (weight 15) - walls act as magnets/barriers
    cw, pw = gex.get("call_wall"), gex.get("put_wall")
    if cw and price and abs(price - cw) / price < 0.004:
        vote -= 15
        factors.append({"name": "Call wall", "detail": f"pinned near call wall {cw} (resistance)", "contribution": -15})
    elif pw and price and abs(price - pw) / price < 0.004:
        vote += 15
        factors.append({"name": "Put wall", "detail": f"supported near put wall {pw}", "contribution": 15})

    vote = max(-100.0, min(100.0, vote))
    confluence = round(abs(vote), 1)

    if vote > 0:
        bias = "BULLISH"
    elif vote < 0:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    if confluence >= BUY_THRESHOLD and bias == "BULLISH":
        action = "BUY CALLS"
    elif confluence >= BUY_THRESHOLD and bias == "BEARISH":
        action = "BUY PUTS"
    else:
        action = "WAIT"

    # human alerts (rule-derived, deterministic)
    alerts = []
    if bb.get("status") == "ABOVE_UPPER":
        alerts.append("Price closed a 5m bar above the upper Bollinger band.")
    if bb.get("status") == "BELOW_LOWER":
        alerts.append("Price closed a 5m bar below the lower Bollinger band.")
    if cw and price and abs(price - cw) / price < 0.002:
        alerts.append(f"Within 0.2% of the 0DTE call wall ({cw}).")
    if pw and price and abs(price - pw) / price < 0.002:
        alerts.append(f"Within 0.2% of the 0DTE put wall ({pw}).")

    return {
        "bias": bias,
        "action": action,
        "confluence_score": confluence,
        "vote": round(vote, 1),
        "factors": factors,
        "alerts": alerts,
    }


# ----------------------------------------------------------------------------
# LLM narrative (LABEL ONLY - never changes the action)
# ----------------------------------------------------------------------------
def build_narrative(metrics: dict) -> dict:
    if not ENABLE_LLM or not ANTHROPIC_API_KEY:
        return {"generated": False, "source": None,
                "text": "LLM narrative disabled.", "structure_alerts": []}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        sys_prompt = (
            "You are a markets desk commentator. You are given pre-computed SPY 0DTE "
            "metrics and a RULE-BASED decision. Do NOT change the decision. Write a "
            "2-3 sentence plain-English narrative explaining the current structure, and "
            "list up to 4 concise structure alerts. Respond ONLY with strict JSON: "
            '{"narrative": "...", "structure_alerts": ["..."]}'
        )
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=500,
            system=sys_prompt,
            messages=[{"role": "user", "content": json.dumps(metrics)}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        # strip code fences if present
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(text)
        return {
            "generated": True,
            "source": CLAUDE_MODEL,
            "text": parsed.get("narrative", ""),
            "structure_alerts": parsed.get("structure_alerts", []),
        }
    except Exception as e:
        return {"generated": False, "source": CLAUDE_MODEL,
                "text": f"(narrative unavailable: {e})", "structure_alerts": []}


# ----------------------------------------------------------------------------
# Mongo
# ----------------------------------------------------------------------------
def build_mongo_uri() -> str:
    """Prefer raw components (auto-encoded); fall back to a full MONGODB_URI."""
    if MONGODB_PASSWORD:
        from urllib.parse import quote_plus
        return (f"mongodb+srv://{quote_plus(MONGODB_USER)}:{quote_plus(MONGODB_PASSWORD)}"
                f"@{MONGODB_CLUSTER_HOST}/?retryWrites=true&w=majority&appName=Cluster0")
    return MONGODB_URI


_MONGO_COLL = None
def get_collection():
    global _MONGO_COLL
    if _MONGO_COLL is not None:
        return _MONGO_COLL
    uri = build_mongo_uri()
    if not uri:
        return None
    from pymongo import MongoClient
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    client.admin.command("ping")
    _MONGO_COLL = client[DB_NAME][COLLECTION_NAME]
    return _MONGO_COLL


def upsert_payload(doc: dict) -> bool:
    coll = get_collection()
    if coll is None:
        print("[warn] no MONGODB_URI - printing payload instead of writing:")
        print(json.dumps(doc, indent=2, default=str))
        return False
    coll.update_one({"_id": DOC_ID}, {"$set": doc}, upsert=True)
    return True


# ----------------------------------------------------------------------------
# One cycle
# ----------------------------------------------------------------------------
def run_cycle() -> dict:
    if DATA_SOURCE == "alpaca":
        source_label = f"Alpaca {ALPACA_FEED.upper()} (real-time 5m)"
        warnings = []
        if ALPACA_FEED == "iex":
            warnings.append("Alpaca free IEX feed covers ~2-3% of consolidated volume; prices track SIP closely but can lag on thin ticks.")
    else:
        source_label = "yfinance (delayed ~15m)"
        warnings = ["yfinance price data is delayed ~15m and rate-limited; not for real-money 0DTE execution."]
    df = fetch_spy_bars(SYMBOL)
    last = float(df["Close"].iloc[-1])
    prev = float(df["Close"].iloc[0])
    vwap = compute_vwap(df)
    bb = compute_bollinger(df, BB_PERIOD, BB_STD)
    gex = fetch_gex(SYMBOL)
    if gex.get("stale"):
        warnings.append("GEX served from cache (free tier = 5 req/day).")
    if not gex.get("available", False) and "reason" in gex:
        warnings.append(f"GEX unavailable: {gex['reason']}")

    signal = compute_rule_signal(last, vwap, bb, gex)

    metrics_for_llm = {
        "symbol": SYMBOL, "price": last, "vwap": round(vwap, 2),
        "bollinger": bb, "gex": gex, "rule_signal": signal,
    }
    narrative = build_narrative(metrics_for_llm)

    now = dt.datetime.utcnow()
    doc = {
        "_id": DOC_ID,
        "symbol": SYMBOL,
        "updated_at": now.isoformat() + "Z",
        "updated_epoch": int(now.timestamp()),
        "data_source": source_label,
        "price": {
            "last": round(last, 2),
            "session_change": round(last - prev, 2),
            "session_change_pct": round((last - prev) / prev * 100, 2) if prev else 0,
        },
        "vwap": {
            "value": round(vwap, 2),
            "price_vs_vwap_pct": round((last - vwap) / vwap * 100, 2) if vwap else 0,
            "position": "ABOVE" if last >= vwap else "BELOW",
        },
        "bollinger": bb,
        "gex": gex,
        "signal": {
            "bias": signal["bias"],
            "action": signal["action"],
            "confluence_score": signal["confluence_score"],
            "factors": signal["factors"],
        },
        "narrative": narrative,
        "alerts": signal["alerts"] + narrative.get("structure_alerts", []),
        "warnings": warnings,
        "disclaimer": "Heuristic decision-support only. Not investment advice. Backtest before trading.",
    }
    return doc


def main():
    print(f"[start] SPY 0DTE worker | loop={LOOP_SECONDS}s | gex_refresh={GEX_REFRESH_SECONDS}s "
          f"| llm={'on' if ENABLE_LLM else 'off'}", flush=True)
    while True:
        t0 = time.time()
        try:
            doc = run_cycle()
            ok = upsert_payload(doc)
            print(f"[{doc['updated_at']}] {doc['symbol']} {doc['price']['last']} "
                  f"bias={doc['signal']['bias']} action={doc['signal']['action']} "
                  f"conf={doc['signal']['confluence_score']} written={ok}", flush=True)
        except Exception as e:
            print(f"[error] cycle failed: {e}", file=sys.stderr, flush=True)
        if RUN_ONCE:
            break
        elapsed = time.time() - t0
        time.sleep(max(1, LOOP_SECONDS - elapsed))


if __name__ == "__main__":
    main()
