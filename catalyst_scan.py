"""catalyst_scan.py — daily catalyst-driven defined-risk option picker.
See project doc claude/catalyst-scan-module.md for the annotated source of record.
"""
import json, math, os, urllib.request, datetime as dt

HDR = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json', 'Referer': 'https://www.cboe.com/'}
MAX_COST = 300.0
OTM_LO, OTM_HI = 2.0, 10.0
OI_MIN, SPREAD_MAX = 500, 20.0
DTE_LO, DTE_HI = 7, 14   # 1-2 weeks out: more runway, less immediate theta decay
TARGET_MULT, STOP_MULT = 2.0, 0.5
WATCH_EXTRA = ['AFRM', 'PDD', 'DG', 'DLTR', 'KSS', 'FL', 'BBWI', 'SOFI', 'PLTR', 'MARA']


def _get(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=HDR), timeout=30))


def earnings_calendar(finnhub_key, days=12):
    today = dt.date.today()
    frm, to = today + dt.timedelta(days=3), today + dt.timedelta(days=days)
    url = f'https://finnhub.io/api/v1/calendar/earnings?from={frm}&to={to}&token={finnhub_key}'
    try:
        rows = _get(url).get('earningsCalendar', [])
    except Exception:
        rows = []
    return [(r['symbol'], r['date']) for r in rows if r.get('symbol')]


def closes(t):
    try:
        u = f'https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=2mo&interval=1d'
        r = _get(u)['chart']['result'][0]['indicators']['quote'][0]['close']
        return [c for c in r if c]
    except Exception:
        return []


def direction(t):
    c = closes(t)
    if len(c) < 21:
        return None, 0
    spot, sma20 = c[-1], sum(c[-20:]) / 20
    roc10 = (c[-1] / c[-11] - 1) * 100
    trend = 'bull' if spot >= sma20 else 'bear'
    agree = (roc10 > 0) if trend == 'bull' else (roc10 < 0)
    conf = min(90, 40 + abs(roc10) * 4) if agree else 25
    return trend, round(conf)


def chain(t):
    try:
        return _get(f'https://cdn.cboe.com/api/global/delayed_quotes/options/{t}.json')['data']
    except Exception:
        return None


def _parse(sym):
    tail = sym[-15:]
    return tail[:6], tail[6], int(tail[7:]) / 1000


def pick_for(t, catalyst_date):
    d = direction(t)
    if not d[0]:
        return None
    want = 'C' if d[0] == 'bull' else 'P'
    data = chain(t)
    if not data:
        return None
    spot = data['current_price']
    cat = dt.date.fromisoformat(catalyst_date)
    best = None
    for o in data['options']:
        yymmdd, cp, k = _parse(o['option'])
        if cp != want:
            continue
        exp = dt.datetime.strptime(yymmdd, '%y%m%d').date()
        dte = (exp - dt.date.today()).days
        if not (DTE_LO <= dte <= DTE_HI) or exp <= cat:
            continue
        a, b, oi = o['ask'], o['bid'], o['open_interest']
        otm = ((k - spot) / spot * 100) if cp == 'C' else ((spot - k) / spot * 100)
        if a <= 0 or not (OTM_LO <= otm <= OTM_HI) or oi < OI_MIN:
            continue
        if (a - b) / a * 100 > SPREAD_MAX:
            continue
        n = int(MAX_COST // (a * 100))
        if n < 1:
            continue
        cost = round(n * a * 100, 2)
        cand = {'underlying': t, 'direction': d[0], 'confidence': d[1],
                'type': 'call' if cp == 'C' else 'put', 'strike': k, 'exp': str(exp), 'dte': dte,
                'otm': round(otm, 1), 'ask': a, 'oi': oi, 'iv': o.get('iv'), 'contracts': n, 'cost': cost,
                'delta': o.get('delta'), 'theta': o.get('theta'), 'gamma': o.get('gamma'),
                'target': round(a * TARGET_MULT, 2), 'stop': round(a * STOP_MULT, 3),
                'catalyst_date': catalyst_date,
                'enter_from': str(cat - dt.timedelta(days=5)), 'enter_to': str(cat - dt.timedelta(days=1)),
                'exit_by': str(cat + dt.timedelta(days=1)), 'iv_ramp_exit': str(cat - dt.timedelta(days=1))}
        cand['_rank'] = (cand['cost'], oi)
        if best is None or cand['_rank'] > best['_rank']:
            best = cand
    if best:
        best.pop('_rank', None)
    return best


def load_json_catalysts(path=None):
    """Read catalysts.json (FDA/PDUFA and other hand-curated events) from disk and
    return [(ticker, 'YYYY-MM-DD'), ...] to feed the same scan loop as earnings.
    Safe: any read/parse error yields an empty list, never raises."""
    path = path or os.environ.get('CATALYSTS_JSON', 'catalysts.json')
    try:
        rows = json.load(open(path))
    except Exception:
        return []
    out = []
    for r in rows:
        t, d = r.get('ticker'), r.get('date')
        if t and d:
            out.append((t, d))
    return out


def scan(finnhub_key, extra_catalysts=None):
    # Primary catalyst list = Finnhub earnings + caller-supplied dates + curated
    # FDA/PDUFA events from catalysts.json. FDA biotech names flow through the exact
    # same 7-14 DTE / <$300 / gate pipeline as earnings.
    names = earnings_calendar(finnhub_key) + list(extra_catalysts or []) + load_json_catalysts()
    names += [(t, str(dt.date.today() + dt.timedelta(days=7))) for t in WATCH_EXTRA]
    seen, picks = set(), []
    for t, cdate in names:
        if t in seen:
            continue
        seen.add(t)
        try:
            p = pick_for(t, cdate)
        except Exception:
            p = None
        if p:
            picks.append(p)
    today = str(dt.date.today())
    picks.sort(key=lambda p: (p['enter_from'] <= today <= p['enter_to'], p['confidence'], p['cost']), reverse=True)
    return picks
