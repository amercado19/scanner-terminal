#!/usr/bin/env python3
"""SPY daily direction model + whale-window calendar. Rules fixed in advance;
score published BEFORE the day and graded after — no post-hoc tuning."""
import json, urllib.request, datetime

UA = {'User-Agent': 'Mozilla/5.0'}
SEC_UA = {'User-Agent': 'ScannerTerminal research andresmercado1919@gmail.com'}

def yq(t, rng='6mo'):
    u = f'https://query1.finance.yahoo.com/v8/finance/chart/{t}?range={rng}&interval=1d'
    d = json.load(urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=20))
    r = d['chart']['result'][0]
    c = [x for x in r['indicators']['quote'][0]['close'] if x is not None]
    return {'price': r['meta'].get('regularMarketPrice'), 'closes': c,
            'prev': c[-2] if len(c) > 1 else r['meta'].get('chartPreviousClose')}

def sma(a, n):
    return sum(a[-n:]) / n if len(a) >= n else None

def spy_direction():
    """Composite 0-100. >=60 bullish, <=40 bearish, else neutral.
    Components fixed 2026-08-13; never re-weighted after seeing outcomes."""
    spy = yq('SPY'); c = spy['closes']; px = spy['price']
    vix = yq('^VIX', '3mo'); v = vix['closes']; vpx = vix['price']
    parts = {}

    # 1. Trend (30): above both MAs = 30, one = 18, neither = 4
    s20, s50 = sma(c, 20), sma(c, 50)
    above = (px > s20) + (px > s50)
    parts['trend'] = [4, 18, 30][above]

    # 2. Momentum (25): 5-day return, scaled
    r5 = 100 * (px / c[-6] - 1) if len(c) > 6 else 0
    parts['momentum'] = max(0, min(25, round(12.5 + r5 * 6)))

    # 3. Volatility regime (20): low + falling VIX = risk-on
    vchg = 100 * (vpx / v[-6] - 1) if len(v) > 6 else 0
    vscore = 20 if vpx < 16 else (14 if vpx < 20 else (7 if vpx < 26 else 2))
    if vchg > 10: vscore = max(0, vscore - 6)
    elif vchg < -10: vscore = min(20, vscore + 4)
    parts['volatility'] = vscore

    # 4. Breadth (15): % of sector ETFs above their own 20-DMA
    secs = ['XLK','XLF','XLE','XLV','XLI','XLY','XLP','XLU']
    ok = 0; n = 0
    for s in secs:
        try:
            d = yq(s, '3mo'); m = sma(d['closes'], 20)
            if m: n += 1; ok += d['price'] > m
        except Exception: pass
    breadth_pct = (ok / n * 100) if n else 50
    parts['breadth'] = round(15 * breadth_pct / 100)

    # 5. News/macro (10): filled from ledger world signals
    parts['news'] = None  # set by caller

    return {'price': px, 'sma20': round(s20,2) if s20 else None,
            'sma50': round(s50,2) if s50 else None,
            'ret5': round(r5,2), 'vix': vpx, 'vix_chg5': round(vchg,1),
            'breadth_pct': round(breadth_pct), 'parts': parts,
            'day_pct': round(100*(px/spy['prev']-1),2) if spy.get('prev') else 0}

def news_score(signals):
    """10 pts: bullish signals minus bearish, centered at 5."""
    if not signals: return 5
    b = sum(1 for s in signals if s.get('sentiment') == 'bullish')
    r = sum(1 for s in signals if s.get('sentiment') == 'bearish')
    return max(0, min(10, 5 + 2 * (b - r)))

def whale_windows(today=None):
    """Filing/flow windows when large-holder information actually surfaces."""
    t = today or datetime.date.today()
    qends = [datetime.date(t.year-1,12,31), datetime.date(t.year,3,31),
             datetime.date(t.year,6,30), datetime.date(t.year,9,30)]
    deadlines = [(q + datetime.timedelta(days=45)) for q in qends]
    nxt = min([d for d in deadlines if d >= t], default=deadlines[0])
    return {
        'next_13f_deadline': nxt.isoformat(),
        'days_to_13f': (nxt - t).days,
        'in_13f_window': (nxt - t).days <= 5,
        'notes': {
            '13F (institutions >$100M)': '45 days after quarter end — Feb 14, May 15, Aug 14, Nov 14. '
                'Positions are up to 4.5 months stale; treat as sentiment, never as a trade signal.',
            'Form 4 (insiders)': 'within 2 business days of the trade — the most timely legal disclosure. '
                'Filing burst typically 4:00-6:30pm ET.',
            'SC 13D (activist >5%)': 'within 10 days of crossing 5% — the highest-signal filing; '
                'often moves the stock on the filing itself.',
            'SC 13G (passive >5%)': 'annual/quarterly amendments; low signal.',
            'Block/dark prints': 'cluster in the opening 30 min (9:30-10:00 ET) and the closing '
                'auction (3:50-4:00 ET). Free feeds do not expose these reliably.',
            'Options sweeps': 'cluster first hour and last hour; requires paid flow data.',
        }
    }

def sec_recent(form, count=40):
    u = (f'https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}'
         f'&dateb=&owner=include&count={count}&output=atom')
    try:
        return urllib.request.urlopen(urllib.request.Request(u, headers=SEC_UA),
                                      timeout=25).read().decode('utf-8', 'ignore')
    except Exception:
        return ''

if __name__ == '__main__':
    L = json.load(open('ledger.json'))
    m = spy_direction()
    m['parts']['news'] = news_score(L['meta'].get('signals', []))
    total = sum(v for v in m['parts'].values() if v is not None)
    m['score'] = total
    m['bias'] = 'BULLISH' if total >= 60 else ('BEARISH' if total <= 40 else 'NEUTRAL')
    m['whale'] = whale_windows(datetime.date(2026, 8, 13))
    print(json.dumps(m, indent=1))
