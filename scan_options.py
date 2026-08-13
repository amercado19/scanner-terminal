"""Wide option scan — build the candidate funnel, score it, take the top N.

Gates are UNCHANGED from day one. This file only widens the funnel and ranks
what clears the gates by confidence. Widening the funnel while holding the
gates fixed is the only honest way to get more signals per day.
"""
import json, math, sys, time, urllib.request
from datetime import date, timedelta
import option_score as OS

HDR = {'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'),
       'Accept': 'application/json', 'Referer': 'https://www.cboe.com/'}

ETF_UNIVERSE = ['SPY', 'QQQ', 'IWM', 'DIA', 'SMH', 'XLE', 'XLF', 'XLK']
BLUECHIP = ['AAPL', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'META', 'TSLA', 'TSM', 'AMD', 'AVGO',
            'NFLX', 'JPM', 'XOM', 'COIN', 'PLTR']


def chain(t):
    url = f'https://cdn.cboe.com/api/global/delayed_quotes/options/{t}.json'
    last = None
    for i in range(4):
        try:
            return json.load(urllib.request.urlopen(
                urllib.request.Request(url, headers=HDR), timeout=40))['data']
        except Exception as e:
            last = e
            time.sleep(15 * (i + 1))
    print(f'  !! {t} chain unavailable: {last}')
    return None


def closes(t, rng='3mo'):
    u = f'https://query1.finance.yahoo.com/v8/finance/chart/{t}?range={rng}&interval=1d'
    try:
        r = json.load(urllib.request.urlopen(
            urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'}), timeout=25))
        return [c for c in r['chart']['result'][0]['indicators']['quote'][0]['close'] if c]
    except Exception:
        return []


def parse(sym):
    tail = sym[-15:]
    return tail[:6], tail[6], int(tail[7:]) / 1000


def candidates(t, direction, tier, exp_yymmdd, entry, exit_by, expiry_iso,
               otm_lo=2.0, otm_hi=10.0, prem_lo=0.05, prem_hi=None, rv=None):
    d = chain(t)
    if not d:
        return []
    spot = d['current_price']
    want = 'C' if direction == 'bull' else 'P'
    out = []
    for o in d['options']:
        exp, cp, k = parse(o['option'])
        if exp != exp_yymmdd or cp != want:
            continue
        a, b, oi, iv = o['ask'], o['bid'], o['open_interest'], o['iv']
        if a <= 0 or a < prem_lo or (prem_hi and a > prem_hi):
            continue
        otm = ((k - spot) / spot * 100) if cp == 'C' else ((spot - k) / spot * 100)
        spr = (a - b) / a * 100
        n = int(250 // (a * 100))
        if not (otm_lo <= otm <= otm_hi and oi >= 500 and spr <= 20 and n >= 1 and n * a * 100 <= 250):
            continue
        sc = OS.score_contract(spot=spot, strike=k, opt_type='call' if cp == 'C' else 'put',
                               ask=a, bid=b, oi=oi, iv=iv, entry_date=entry, exit_by=exit_by,
                               expiry=expiry_iso, signal_tier=tier, realized_vol=rv)
        out.append({'underlying': t, 'spot': round(spot, 2), 'strike': k,
                    'type': 'call' if cp == 'C' else 'put', 'ask': a, 'bid': b, 'oi': oi,
                    'iv': iv, 'otm': round(otm, 2), 'spread_pct': round(spr, 1),
                    'contracts_n': n, 'cost': round(n * a * 100, 2),
                    'expiry': expiry_iso, 'exit_by': exit_by, 'score': sc})
    out.sort(key=lambda x: -x['score']['total'])
    return out[:3]
