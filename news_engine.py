#!/usr/bin/env python3
"""Free news engine for the trading terminal.

Sources (all zero-cost):
  - Finnhub general/market news  (free tier, needs FINNHUB_KEY)
  - RSS via feedparser: SEC EDGAR (8-K, Form 4), Yahoo Finance, PR Newswire, FDA

Each headline is run through a keyword IMPACT RULES engine that produces:
  impacted tickers/sectors, a directional bias (bullish/bearish) per item, and a
  1-2 sentence reasoning string. (Hook: replace classify() with an LLM call for
  richer reasoning where an API key is available.)

Also exposes dispatch_breaking() to push gated, de-duplicated Discord alerts.

Everything degrades gracefully: any network/parse failure yields [] rather than
raising, so the bake/build never breaks because a feed was down.
"""
import json, os, re, time, hashlib, urllib.request

try:
    import feedparser
    HAVE_FEEDPARSER = True
except Exception:
    HAVE_FEEDPARSER = False

UA = {"User-Agent": "Mozilla/5.0 (scanner-terminal news bot)"}

RSS_FEEDS = {
    "SEC 8-K": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&company=&dateb=&owner=include&count=40&output=atom",
    "SEC Form 4": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb=&owner=include&count=40&output=atom",
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "PR Newswire": "https://www.prnewswire.com/rss/news-releases-list.rss",
    "FDA Press": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
}

# company name -> ticker (extend as needed)
COMPANY_MAP = {
    "nvidia": "NVDA", "apple": "AAPL", "microsoft": "MSFT", "tesla": "TSLA", "amazon": "AMZN",
    "meta": "META", "alphabet": "GOOGL", "google": "GOOGL", "moderna": "MRNA", "pfizer": "PFE",
    "amd": "AMD", "broadcom": "AVGO", "crowdstrike": "CRWD", "palantir": "PLTR", "coinbase": "COIN",
    "marvell": "MRVL", "gilead": "GILD", "jazz pharmaceuticals": "JAZZ",
}

# Each rule: (keyword regex, sentiment, [ (ticker/sector, bias, reason) ... ], high_impact)
IMPACT_RULES = [
    (r"\b(rate cut|dovish|cuts? rates?|easing)\b", "bullish",
     [("QQQ", "bullish", "Lower rates raise the present value of long-duration growth cash flows."),
      ("NVDA", "bullish", "Rate cuts favor high-multiple tech/AI leaders."),
      ("KRE", "bearish", "Cuts compress regional-bank net interest margins.")], True),
    (r"\b(rate hike|hawkish|hotter|inflation.*(rose|jumps|hot)|raises? rates?)\b", "bearish",
     [("QQQ", "bearish", "Higher-for-longer rates pressure long-duration growth valuations."),
      ("XLF", "bullish", "Higher rates can widen bank net interest margins.")], True),
    (r"\b(fda approv|approves|clears?|positive (results|data|readout)|meets? (primary )?endpoint)\b", "bullish",
     [("XBI", "bullish", "Approvals/positive readouts de-risk the biotech complex.")], True),
    (r"\b(crl|complete response letter|fails? (to|the)|trial (fail|halt)|rejected by the fda)\b", "bearish",
     [("XBI", "bearish", "Rejections/trial failures raise perceived biotech regulatory risk.")], True),
    (r"\b(opec|oil (supply|cut|jumps)|crude (spikes|surges)|barrel)\b", "bullish",
     [("XLE", "bullish", "Tighter supply / higher crude lifts energy producer margins.")], False),
    (r"\b(beats? (on )?earnings|tops? estimates|record (revenue|quarter)|raises? guidance)\b", "bullish",
     [("", "bullish", "An earnings beat / raised guidance typically lifts the reporting name.")], True),
    (r"\b(misses? (on )?earnings|cuts? guidance|guidance cut|falls? short|warns?)\b", "bearish",
     [("", "bearish", "A miss / cut guidance typically pressures the reporting name.")], True),
    (r"\b(layoffs?|restructur|job cuts?)\b", "bearish",
     [("", "bearish", "Layoffs signal demand weakness though can aid near-term margins.")], False),
    (r"\b(ai|chip|semiconductor|data center|gpu) (demand|boom|shortage|orders?)\b", "bullish",
     [("SMH", "bullish", "Stronger AI/chip demand flows through the semiconductor complex."),
      ("NVDA", "bullish", "NVDA is the primary beneficiary of AI compute demand.")], False),
    (r"\b(8-k|material (event|agreement)|merger|acquisition|to acquire|buyout)\b", "bullish",
     [("", "bullish", "M&A / material 8-K events are typically catalysts for the named company.")], True),
    (r"\b(insider (buy|purchase)|form 4)\b", "neutral",
     [("", "neutral", "Insider transactions are sentiment signals, not standalone trade triggers.")], False),
]

_TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b|\(([A-Z]{2,5})\)|\b(NYSE|NASDAQ):\s*([A-Z]{1,5})\b")


def _extract_tickers(text):
    out = set()
    for m in _TICKER_RE.finditer(text or ""):
        out.add(next(g for g in m.groups() if g))
    low = (text or "").lower()
    for name, tk in COMPANY_MAP.items():
        if name in low:
            out.add(tk)
    return list(out)


def classify(headline, body=""):
    """Return {'sentiment', 'breaking', 'impact': [{ticker, sector, bias, reason}]}."""
    text = f"{headline} {body}"
    tickers = _extract_tickers(text)
    impact, sentiments, breaking = [], [], False
    for rx, sent, effects, high in IMPACT_RULES:
        if re.search(rx, text, re.I):
            sentiments.append(sent)
            breaking = breaking or high
            for tk, bias, reason in effects:
                # blank ticker => attribute to the headline's own tickers
                targets = [tk] if tk else (tickers or ["(named co.)"])
                for tgt in targets:
                    impact.append({"ticker": tgt, "sector": _sector_of(tgt),
                                   "bias": bias, "reason": reason})
    # de-dup impact by (ticker,bias)
    seen, uniq = set(), []
    for i in impact:
        k = (i["ticker"], i["bias"])
        if k not in seen:
            seen.add(k); uniq.append(i)
    pos = sentiments.count("bullish"); neg = sentiments.count("bearish")
    sentiment = "bullish" if pos > neg else "bearish" if neg > pos else "neutral"
    return {"sentiment": sentiment, "breaking": breaking, "impact": uniq}


_SECTORS = {"QQQ": "Tech/Growth", "NVDA": "Semis/AI", "SMH": "Semiconductors", "XLE": "Energy",
            "XLF": "Financials", "KRE": "Regional Banks", "XBI": "Biotech", "SPY": "Broad Market"}


def _sector_of(tk):
    return _SECTORS.get(tk, "")


def _http_json(url, timeout=15):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.load(r)


def fetch_finnhub(key, category="general", limit=15):
    if not key:
        return []
    try:
        rows = _http_json(f"https://finnhub.io/api/v1/news?category={category}&token={key}")[:limit]
    except Exception:
        return []
    out = []
    for r in rows:
        out.append({"headline": r.get("headline", ""), "body": r.get("summary", ""),
                    "source": r.get("source", "Finnhub"), "url": r.get("url", ""),
                    "epoch": r.get("datetime", 0)})
    return out


def fetch_rss(feeds=None, per_feed=8):
    if not HAVE_FEEDPARSER:
        return []
    feeds = feeds or RSS_FEEDS
    out = []
    for name, url in feeds.items():
        try:
            raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=8).read()
            d = feedparser.parse(raw)
            for e in d.entries[:per_feed]:
                out.append({"headline": e.get("title", ""), "body": e.get("summary", "")[:400],
                            "source": name, "url": e.get("link", ""),
                            "epoch": int(time.mktime(e.published_parsed)) if e.get("published_parsed") else 0})
        except Exception:
            continue
    return out


def _reltime(epoch):
    if not epoch:
        return "recent"
    dt = max(0, int(time.time()) - int(epoch))
    if dt < 3600:
        return f"{dt // 60}m"
    if dt < 86400:
        return f"{dt // 3600}h"
    return f"{dt // 86400}d"


def build_news(finnhub_key=None, feeds=None, limit=14):
    """Aggregate + classify into dashboard-shaped news items, most impactful first."""
    raw = fetch_finnhub(finnhub_key) + fetch_rss(feeds)
    seen, items = set(), []
    for r in raw:
        h = (r.get("headline") or "").strip()
        if not h:
            continue
        key = hashlib.md5(h.lower().encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        c = classify(h, r.get("body", ""))
        items.append({
            "breaking": c["breaking"], "sentiment": c["sentiment"],
            "ticker": (c["impact"][0]["ticker"] if c["impact"] else ""),
            "headline": h, "source": r.get("source", ""), "time": _reltime(r.get("epoch")),
            "body": (r.get("body") or "")[:280], "url": r.get("url", ""),
            "impact": c["impact"], "_epoch": r.get("epoch", 0),
        })
    items.sort(key=lambda x: (x["breaking"], len(x["impact"]), x["_epoch"]), reverse=True)
    for it in items:
        it.pop("_epoch", None)
    return items[:limit]


def dispatch_breaking(items, webhook_url=None, state_path=".news_alert_state.json"):
    """Push de-duplicated Discord alerts for breaking items. No-op without a webhook."""
    webhook_url = webhook_url or os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        return 0
    try:
        seen = set(json.load(open(state_path)))
    except Exception:
        seen = set()
    sent = 0
    for it in items:
        if not it.get("breaking"):
            continue
        key = hashlib.md5(it["headline"].lower().encode()).hexdigest()
        if key in seen:
            continue
        tk = ", ".join(f"{i['ticker']} {'+' if i['bias'] == 'bullish' else '-' if i['bias'] == 'bearish' else '•'}"
                       for i in it.get("impact", [])[:4])
        msg = f"🚨 **BREAKING** ({it['sentiment'].upper()}): {it['headline']}\nImpact: {tk or 'broad'}\n{it.get('url', '')}"
        try:
            body = json.dumps({"content": msg}).encode()
            urllib.request.urlopen(urllib.request.Request(
                webhook_url, data=body, headers={"Content-Type": "application/json"}), timeout=10)
            seen.add(key); sent += 1
        except Exception:
            continue
    try:
        json.dump(sorted(seen), open(state_path, "w"))
    except Exception:
        pass
    return sent


if __name__ == "__main__":
    news = build_news(os.environ.get("FINNHUB_KEY", ""))
    print(json.dumps(news[:5], indent=2))
    print(f"{len(news)} items | breaking: {sum(1 for n in news if n['breaking'])}")
