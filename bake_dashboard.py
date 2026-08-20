#!/usr/bin/env python3
"""Bake window.DASH_DATA (from ledger.json + catalysts.json) into the SPA and set the
live API base. Network-free: the SPY tiles are filled at runtime from /api/spy; this
only bakes the structural tabs (portfolio, option contracts, news, catalysts).

Usage: python3 bake_dashboard.py <template.html> <ledger.json> <catalysts.json> <out.html>
"""
import json, re, sys, datetime as dt

API_BASE = "https://scanner-terminal-1.onrender.com"


def num(x):
    return x if isinstance(x, (int, float)) else None


# Single source of truth: import the trailing-stop thresholds from build_dashboard.py.
try:
    from build_dashboard import TRAILING_STOP_ARM_PCT as TRAIL_ACTIVATE, \
        TRAILING_STOP_TRAIL_PCT as TRAIL_PCT
except Exception:  # standalone use / build_dashboard not importable
    TRAIL_ACTIVATE, TRAIL_PCT = 0.30, 0.20


def trailing(o):
    """Trailing-stop display fields. Uses engine-stored peak/trail when present
    (build_dashboard.update_options), else estimates from the current mark so the
    snapshot still shows real numbers."""
    entry = o.get("entry_ask") or 0.0
    c = o.get("contracts_n", 1) or 1
    plpct = o.get("pl_pct", 0) or 0
    peak_prem = o.get("peak_premium")
    if peak_prem is None:
        peak_prem = entry * (1 + max(plpct, 0) / 100.0)
    peak_pct = o.get("peak_pl_pct")
    if peak_pct is None:
        peak_pct = round(100 * (peak_prem / entry - 1), 1) if entry else 0.0
    armed = o.get("trail_active")
    if armed is None:
        armed = peak_pct >= TRAIL_ACTIVATE * 100
    tstop = o.get("trail_stop_premium")
    if armed and not tstop:
        tstop = round(peak_prem * (1 - TRAIL_PCT), 3)
    hard = o.get("stop_premium") or round(entry * 0.5, 3)
    eff = max(hard, tstop) if (armed and tstop) else hard
    return {"peakPct": peak_pct, "trailActive": bool(armed),
            "trailStopPrem": round(eff, 3), "trailStopValue": round(eff * 100 * c, 2),
            "hardStopValue": round(hard * 100 * c, 2)}


def _edge_from(o):
    """Edge score for the card: use a stored score.edge if present, else derive from the
    HV-rank / delta / volume signals on the position. Returns None when no signal exists
    (so the card renders no badge rather than a misleading 0)."""
    e = o.get("edge")
    if isinstance(e, (int, float)):
        return int(e)
    sc = o.get("score")
    if isinstance(sc, dict) and isinstance(sc.get("edge"), (int, float)):
        return int(sc["edge"])
    hv, dl, vo = o.get("iv_rank_proxy"), o.get("delta"), o.get("vol_oi_ratio")
    if hv is None and dl is None and vo is None:
        return None
    edge = 0
    if isinstance(hv, (int, float)):
        edge += 10 if hv <= 35 else (-10 if hv >= 75 else 0)
    if isinstance(dl, (int, float)) and 0.30 <= abs(dl) <= 0.55:
        edge += 10
    if isinstance(vo, (int, float)) and vo >= 2.0:
        edge += 10
    return edge


def option_card(o):
    spot = o.get("entry_spot")
    strike = o.get("strike")
    otm = None
    if isinstance(spot, (int, float)) and spot and isinstance(strike, (int, float)):
        otm = round((strike - spot) / spot * 100, 1) if o.get("type") == "call" else round((spot - strike) / spot * 100, 1)
    cd = o.get("conf_detail", {}) or {}
    card = {
        "ticker": o.get("underlying"), "kind": o.get("type", "call"), "strike": strike,
        "exp": o.get("expiry"), "entry": o.get("entry_ask"), "target": o.get("target_premium"),
        "stop": o.get("stop_premium"), "contracts": o.get("contracts_n", 1), "cost": o.get("cost"),
        "conf": o.get("conf", 0), "spot": spot, "iv": num(o.get("iv")), "otm": otm,
        "ivRank": (o.get("iv_rank_proxy") / 100.0) if isinstance(o.get("iv_rank_proxy"), (int, float)) else None,
        "delta": num(o.get("delta")),
        "oi": num(o.get("oi")), "em": num(cd.get("expected_move_pct")),
        "reqMove": num(cd.get("required_move_pct")), "spreadPct": num(cd.get("spread_pct")),
        "exitBy": o.get("exit_by"),
        "thesis": o.get("note") or o.get("source_call") or "Open contract — trails 20% below peak, uncapped upside.",
    }
    tr = trailing(o)
    card.update({"peakPct": tr["peakPct"], "trailActive": tr["trailActive"],
                 "trailStop": tr["trailStopPrem"], "uncapped": True, "edge": _edge_from(o)})
    return {k: v for k, v in card.items() if v is not None}


def max_drawdown(history):
    peak, dd = None, 0.0
    for h in history:
        v = h.get("value")
        if not isinstance(v, (int, float)):
            continue
        peak = v if peak is None else max(peak, v)
        if peak:
            dd = min(dd, v / peak - 1)
    return round(dd * 100, 1)


SAMPLE_NEWS = [
    {"breaking": True, "sentiment": "bullish", "ticker": "QQQ",
     "headline": "Fed minutes hint at a September rate cut", "source": "Macro wire", "time": "2h",
     "body": "Officials signaled openness to easing as inflation cools.", "url": "",
     "impact": [
         {"ticker": "QQQ", "sector": "Tech/Growth", "bias": "bullish", "reason": "Lower rates lift long-duration growth valuations."},
         {"ticker": "NVDA", "sector": "Semis/AI", "bias": "bullish", "reason": "High-multiple AI leaders benefit most from cuts."},
         {"ticker": "KRE", "sector": "Regional Banks", "bias": "bearish", "reason": "Cuts compress bank net interest margins."}]},
    {"breaking": True, "sentiment": "bullish", "ticker": "XBI",
     "headline": "FDA approves a closely-watched oncology therapy", "source": "FDA Press", "time": "4h",
     "body": "Regulatory clearance de-risks the sponsor and peers.", "url": "",
     "impact": [{"ticker": "XBI", "sector": "Biotech", "bias": "bullish", "reason": "Approvals de-risk the biotech complex."}]},
    {"breaking": False, "sentiment": "bearish", "ticker": "XLE",
     "headline": "Crude slips as OPEC+ signals higher output", "source": "PR Newswire", "time": "6h",
     "body": "Additional barrels pressure energy producer margins.", "url": "",
     "impact": [{"ticker": "XLE", "sector": "Energy", "bias": "bearish", "reason": "More supply pressures producer margins."}]},
    {"breaking": False, "sentiment": "neutral", "ticker": "",
     "headline": "SEC 8-K: mid-cap announces board change", "source": "SEC 8-K", "time": "1h",
     "body": "Governance update with limited immediate price impact.", "url": "",
     "impact": [{"ticker": "(named co.)", "sector": "", "bias": "neutral", "reason": "Governance 8-Ks rarely move price on their own."}]},
]


def _news_from_signals(signals):
    try:
        import news_engine as NE
    except Exception:
        NE = None
    out = []
    for s in signals:
        title = s.get("title") or s.get("headline") or ""
        if not title:
            continue
        c = NE.classify(title, s.get("body", "")) if NE else {"sentiment": s.get("sentiment", "neutral"), "breaking": False, "impact": []}
        out.append({"breaking": bool(s.get("breaking")) or c.get("breaking", False),
                    "sentiment": c.get("sentiment", s.get("sentiment", "neutral")),
                    "ticker": (c["impact"][0]["ticker"] if c.get("impact") else ""),
                    "headline": title, "source": s.get("sector", "world signal"), "time": "today",
                    "body": s.get("body", ""), "url": "", "impact": c.get("impact", [])})
    return out


def _news_sentiment(items):
    if not items:
        return 0.5
    pos = sum(1 for n in items if n.get("sentiment") == "bullish")
    neg = sum(1 for n in items if n.get("sentiment") == "bearish")
    return round(0.5 + 0.5 * ((pos - neg) / len(items)), 3)


def _news_label(s):
    return "Bullish" if s >= 0.6 else "Bearish" if s <= 0.4 else "Neutral"


def build_dash(ledger, catalysts):
    all_ops = ledger.get("options_positions", [])
    closed = ledger.get("closed_options", [])
    meta = ledger.get("meta", {})

    # Archive expired contracts: anything past its expiry leaves the active tables
    # and moves to the closed/historical ledger.
    today = dt.date.today().isoformat()
    ops = [o for o in all_ops if not o.get("expiry") or o.get("expiry") >= today]
    expired = [o for o in all_ops if o.get("expiry") and o.get("expiry") < today]

    def pos_row(o):
        tr = trailing(o)
        return {
            "ticker": o.get("underlying"),
            "strategy": (o.get("source_call", "") or o.get("type", "call").upper())[:42],
            "entryDate": o.get("entry_date"), "exp": o.get("expiry"), "cost": o.get("cost"),
            "value": o.get("value", o.get("cost")),
            "peakPct": tr["peakPct"], "trailActive": tr["trailActive"],
            "trailStop": tr["trailStopValue"], "hardStop": tr["hardStopValue"],
            "plPct": o.get("pl_pct", 0), "kind": o.get("type", "call"),
        }
    positions = [pos_row(o) for o in ops]

    closed_rows = [{
        "ticker": o.get("underlying"), "strategy": o.get("type", "call").upper(),
        "entryDate": o.get("entry_date"), "exitDate": o.get("exit_date"),
        "plPct": o.get("pl_pct", 0), "result": o.get("outcome", "loss"),
        "reason": o.get("exit_reason", ""),
    } for o in closed]
    # append auto-archived expired positions
    for o in expired:
        pl = o.get("pl", 0) or 0
        closed_rows.append({
            "ticker": o.get("underlying"), "strategy": o.get("type", "call").upper(),
            "entryDate": o.get("entry_date"), "exitDate": o.get("expiry"),
            "plPct": o.get("pl_pct", -100.0), "result": "win" if pl > 0 else "loss",
            "reason": "expired (auto-archived)",
        })

    wins = sum(1 for r in closed_rows if r.get("result") == "win")
    realized = round(sum(o.get("pl", 0) or 0 for o in closed), 2)
    unreal = round(sum(o.get("pl", 0) or 0 for o in ops), 2)
    cash = round(sum((meta.get("book_cash", {}) or {}).values()), 2)

    # ---- news: ledger-provided (from build_dashboard) first, else live fetch, else sample ----
    news_items = meta.get("news_items") or []
    if not news_items:
        try:
            import news_engine
            news_items = news_engine.build_news(meta.get("finnhub_key", ""))
            if news_items:
                news_engine.dispatch_breaking(news_items)  # gated: no-op without DISCORD_WEBHOOK_URL
        except Exception as e:
            print(f"WARN news_engine unavailable ({e}); using ledger signals / sample", file=sys.stderr)
    if not news_items:
        news_items = _news_from_signals(meta.get("signals", []) or []) or SAMPLE_NEWS

    imp = {"high": "high", "medium": "med", "med": "med", "low": "low"}
    cats = []
    for c in catalysts:
        if c.get("ticker") and c.get("date"):
            cats.append({"date": c["date"], "ticker": c["ticker"], "type": "FDA/PDUFA",
                         "impact": imp.get(str(c.get("impact", "")).lower(), "med"),
                         "setup": c.get("event", "FDA/PDUFA event"), "vol": c.get("impact", "High")})
    cats.sort(key=lambda x: x["date"])

    def valid_tk(t):
        return bool(t) and bool(re.match(r"^[A-Z]{1,5}$", str(t)))
    wl_tickers = {(w.get("ticker") if isinstance(w, dict) else w) for w in ledger.get("watchlist", [])}
    wl = sorted({t for t in ([o.get("underlying") for o in ops]
                             + [c["ticker"] for c in cats] + list(wl_tickers)) if valid_tk(t)})

    return {
        "meta": {"portfolioLabel": "Paper portfolio", "asOf": meta.get("last_scan", ""), "mode": "baked"},
        "portfolio": {
            "capital": (meta.get("portfolio", {}) or {}).get("capital", 1000), "cash": cash,
            "realized": realized, "unrealized": unreal,
            "winRate": (wins / len(closed_rows)) if closed_rows else 0.0, "winRateN": len(closed_rows),
            "drawdown": max_drawdown(ledger.get("history", [])),
            "positions": positions, "closed": closed_rows,
        },
        # technicals/chain are placeholders — /api/spy overwrites them live at runtime
        "technicals": {"spySpot": None, "spyDayPct": None, "vwapDelta": None, "bollingerPctB": None, "em0dte": {}},
        "chain": {"atmIV": None, "pcRatio": None, "callWall": None, "putWall": None, "gammaFlip": None},
        "options": [option_card(o) for o in ops],
        "news": {"sentiment": _news_sentiment(news_items),
                 "sentimentLabel": _news_label(_news_sentiment(news_items)),
                 "breakingCount": sum(1 for n in news_items if n.get("breaking")),
                 "items": news_items},
        "catalysts": cats, "watchlist": wl,
    }


def main(tpl, ledger_path, cat_path, out):
    html = open(tpl, encoding="utf-8").read()
    ledger = json.load(open(ledger_path))
    try:
        catalysts = json.load(open(cat_path))
    except Exception:
        catalysts = []
    dash = build_dash(ledger, catalysts)
    # strip any prior injected block, then inject fresh (idempotent)
    html = re.sub(r"<script>window\.SCANNER_API_BASE=.*?</script>", "", html, flags=re.S)
    inject = "<script>window.SCANNER_API_BASE=%s;window.DASH_DATA=%s;</script>" % (
        json.dumps(API_BASE), json.dumps(dash))
    html = html.replace("</head>", inject + "</head>", 1)
    open(out, "w", encoding="utf-8").write(html)
    print("baked -> %s | options=%d positions=%d closed=%d catalysts=%d watchlist=%d"
          % (out, len(dash["options"]), len(dash["portfolio"]["positions"]),
             len(dash["portfolio"]["closed"]), len(dash["catalysts"]), len(dash["watchlist"])))


if __name__ == "__main__":
    main(*sys.argv[1:5])
