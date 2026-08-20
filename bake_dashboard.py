#!/usr/bin/env python3
"""Bake window.DASH_DATA (from ledger.json + catalysts.json) into the SPA and set the
live API base. Network-free: the SPY tiles are filled at runtime from /api/spy; this
only bakes the structural tabs (portfolio, option contracts, news, catalysts).

Usage: python3 bake_dashboard.py <template.html> <ledger.json> <catalysts.json> <out.html>
"""
import json, re, sys

API_BASE = "https://scanner-terminal-1.onrender.com"


def num(x):
    return x if isinstance(x, (int, float)) else None


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
        "oi": num(o.get("oi")), "em": num(cd.get("expected_move_pct")),
        "reqMove": num(cd.get("required_move_pct")), "spreadPct": num(cd.get("spread_pct")),
        "exitBy": o.get("exit_by"),
        "thesis": o.get("note") or o.get("source_call") or "Open contract tracked to target/stop.",
    }
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


def build_dash(ledger, catalysts):
    ops = ledger.get("options_positions", [])
    closed = ledger.get("closed_options", [])
    meta = ledger.get("meta", {})
    wins = sum(1 for o in closed if o.get("outcome") == "win")

    positions = [{
        "ticker": o.get("underlying"),
        "strategy": (o.get("source_call", "") or o.get("type", "call").upper())[:42],
        "entryDate": o.get("entry_date"), "cost": o.get("cost"),
        "value": o.get("value", o.get("cost")),
        "target": round((o.get("target_premium") or 0) * 100 * o.get("contracts_n", 1), 2) or None,
        "stop": round((o.get("stop_premium") or 0) * 100 * o.get("contracts_n", 1), 2) or None,
        "plPct": o.get("pl_pct", 0), "kind": o.get("type", "call"),
    } for o in ops]

    closed_rows = [{
        "ticker": o.get("underlying"), "strategy": o.get("type", "call").upper(),
        "entryDate": o.get("entry_date"), "exitDate": o.get("exit_date"),
        "plPct": o.get("pl_pct", 0), "result": o.get("outcome", "loss"),
        "reason": o.get("exit_reason", ""),
    } for o in closed]

    realized = round(sum(o.get("pl", 0) or 0 for o in closed), 2)
    unreal = round(sum(o.get("pl", 0) or 0 for o in ops), 2)
    cash = round(sum((meta.get("book_cash", {}) or {}).values()), 2)

    signals = meta.get("signals", []) or []
    news_items = []
    for s in signals:
        title = s.get("title") or s.get("headline") or ""
        news_items.append({
            "breaking": bool(s.get("breaking")) or (s.get("sector") == "CATALYST"),
            "sentiment": s.get("sentiment", "neutral"),
            "ticker": (title.split(" ")[0] if title and title.split(" ")[0].isupper() else ""),
            "headline": title, "source": s.get("sector", "world signal"),
            "time": "today", "body": s.get("body", ""),
        })

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
            "winRate": (wins / len(closed)) if closed else 0.0, "winRateN": len(closed),
            "drawdown": max_drawdown(ledger.get("history", [])),
            "positions": positions, "closed": closed_rows,
        },
        # technicals/chain are placeholders — /api/spy overwrites them live at runtime
        "technicals": {"spySpot": None, "spyDayPct": None, "vwapDelta": None, "bollingerPctB": None, "em0dte": {}},
        "chain": {"atmIV": None, "pcRatio": None, "callWall": None, "putWall": None, "gammaFlip": None},
        "options": [option_card(o) for o in ops],
        "news": {"sentiment": meta.get("news_sentiment", 0.5),
                 "sentimentLabel": meta.get("news_sentiment_label", "Neutral"), "items": news_items},
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
