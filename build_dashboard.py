#!/usr/bin/env python3
"""News + IPO Scanner v3 — four books, direction calls, targets, terminal UI.

Usage: python3 build_dashboard.py ledger.json out.html
Books: core (news+IPO), vol24 (24h volatility w/ bull/bear call, 5-day eval),
penny ($0.50-$5 listed), longterm (written criteria, 1-3y).
- Sets flag_price / spy_flag_price where flag_price is null.
- Refreshes prices; $1k P/L; alpha vs same-day SPY; enforces stops.
- Evaluates vol24 direction calls at eval_date (hit = sign matches call).
- Interpolates 3m/6m targets from flag-time 12m bull/base/bear (sqrt-time).
- Appends daily history snapshot; renders self-contained terminal dashboard.
"""
import json, math, sys, time, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

def yahoo(t, rng='3mo'):
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{t}?range={rng}&interval=1d'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    d = json.load(urllib.request.urlopen(req, timeout=20))
    res = d['chart']['result'][0]
    q = res['indicators']['quote'][0]['close']
    ts = res['timestamp']
    closes = [(ts[i], round(c, 4)) for i, c in enumerate(q) if c is not None]
    return {'price': res['meta'].get('regularMarketPrice'), 'closes': closes}

def cboe_chain(t):
    """CBOE rate-limits (HTTP 429) when several requests land close together.
    Un-retried 429s are what produced empty option scans on 2026-08-13 —
    the gates were fine, the fetch just died. Back off and retry."""
    url = f'https://cdn.cboe.com/api/global/delayed_quotes/options/{t}.json'
    hdr = {'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'),
           'Accept': 'application/json', 'Referer': 'https://www.cboe.com/'}
    last = None
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers=hdr)
            return json.load(urllib.request.urlopen(req, timeout=40))['data']
        except Exception as e:
            last = e
            time.sleep(20 * (i + 1))
    raise last

def update_options(ledger, today):
    """Mark open option positions to bid via CBOE; enforce trailing-stop / expiry exits.

    Profit maximization (replaces the old fixed 6x target cap):
      - hard stop at -50% of entry protects capital until the trade works;
      - once the contract is +30%, a trailing stop arms and ratchets 20% below the
        peak contract value, locking gains while letting winners run uncapped;
      - losers are cut on the exit_by time-stop; a trade with the trail armed is NOT
        force-closed on time — it runs until the trailing stop or expiry.
    """
    ledger.setdefault('options_positions', []); ledger.setdefault('closed_options', [])
    still = []
    chains = {}
    for o in ledger['options_positions']:
        if o.get('status') == 'closed':
            ledger['closed_options'].append(o); continue
        u = o['underlying']
        try:
            if u not in chains:
                chains[u] = {c['option']: c for c in cboe_chain(u)['options']}
                time.sleep(0.4)
            q = chains[u].get(o['contract'])
            if q:
                o['current_bid'] = q.get('bid') or 0.0
                o['current_ask'] = q.get('ask') or 0.0
        except Exception as e:
            print(f"WARN option {o['contract']}: {e}", file=sys.stderr)
        o.setdefault('target_premium', round(o['entry_ask'] * 6, 2))  # legacy upside REFERENCE only — no longer a hard cap
        o.setdefault('stop_premium', round(o['entry_ask'] * 0.5, 3))  # hard -50% capital-protection stop
        TRAIL_ACTIVATE = 0.30   # arm the trailing stop once the contract is +30%
        TRAIL_PCT = 0.20        # then trail 20% below the peak contract value
        bid = o.get('current_bid', 0.0)
        entry = o['entry_ask']
        # peak contract value seen so far (ratchets up only)
        o['peak_premium'] = round(max(o.get('peak_premium', entry), bid), 3)
        o['peak_pl_pct'] = round(100 * (o['peak_premium'] / entry - 1), 1) if entry else 0.0
        hard_stop = o['stop_premium']
        armed = bool(o.get('trail_active', False) or (bid >= entry * (1 + TRAIL_ACTIVATE)))
        o['trail_active'] = armed
        if armed:
            new_trail = round(o['peak_premium'] * (1 - TRAIL_PCT), 3)
            o['trail_stop_premium'] = round(max(o.get('trail_stop_premium') or 0.0, new_trail), 3)
            eff_stop = max(hard_stop, o['trail_stop_premium'])
        else:
            o['trail_stop_premium'] = None
            eff_stop = hard_stop
        o['effective_stop_premium'] = round(eff_stop, 3)
        o['value'] = round(bid * 100 * o['contracts_n'], 2)
        o['pl'] = round(o['value'] - o['cost'], 2)
        o['pl_pct'] = round(100 * o['pl'] / o['cost'], 1) if o['cost'] else 0.0
        expired = today > o['expiry']
        hit_stop = bid > 0 and bid <= eff_stop
        # losers are cut on the time-stop; a winner with the trail armed runs until the trail or expiry
        time_exit = (today >= o.get('exit_by', '9999')) and not armed
        if hit_stop or expired or time_exit:
            o['status'] = 'closed'
            o['exit_date'] = today
            o['exit_bid'] = 0.0 if expired else bid
            if expired:
                o['value'] = 0.0; o['pl'] = -o['cost']; o['pl_pct'] = -100.0
            o['exit_reason'] = ('trailing stop' if (hit_stop and armed) else
                                'hard stop -50%' if hit_stop else
                                'expired worthless' if expired else
                                'time exit (no profit)')
            o['outcome'] = 'win' if o['pl'] > 0 else 'loss'
            ledger['closed_options'].append(o)
        else:
            still.append(o)
    ledger['options_positions'] = still
    resolved = ledger['closed_options']
    return {'open': len(still), 'resolved': len(resolved),
            'wins': sum(1 for o in resolved if o.get('outcome') == 'win'),
            'spent': round(sum(o['cost'] for o in still + resolved), 2),
            'open_value': round(sum(o.get('value', 0) for o in still), 2),
            'recovered': round(sum(o.get('value', 0) if o['status'] == 'closed' else 0 for o in resolved), 2),
            'total_pl': round(sum(o.get('pl', 0) for o in still + resolved), 2)}

def interp_targets(p):
    if not p.get('t12'):
        return
    cur = p['flag_price']
    out = {}
    for h, k in ((3, 't3'), (6, 't6'), (12, 't12i')):
        f = math.sqrt(h / 12)
        out[k] = {kk: round(cur + (p['t12'][kk] - cur) * f, 2) for kk in ('bull', 'base', 'bear')}
    p['targets'] = out

def main(ledger_path, out_path):
    ledger = json.load(open(ledger_path))
    ledger.setdefault('history', []); ledger.setdefault('closed', [])
    port = ledger['meta'].get('portfolio', {})
    bands = port.get('band_stakes', {})
    cash = ledger['meta'].setdefault('book_cash', {})
    def band_stake(p):
        book = p.get('book', 'core'); sc = p.get('score_total', 50)
        base = bands.get(book, 30)
        if isinstance(base, dict):
            base = base['strong'] if sc >= 70 else (base['spec'] if sc < 50 else base['standard'])
        avail = cash.get(book, 0)
        st = round(min(base, avail), 2)
        cash[book] = round(avail - st, 2)
        return st
    now = datetime.now(timezone.utc)
    # Display everything in Eastern, 12-hour clock — this is a US-market tool and
    # nobody reads "15:21 UTC" as "just after lunch".
    et = now.astimezone(ZoneInfo('America/New_York'))
    stamp = et.strftime('%b %d, %Y · %I:%M %p ET').replace(' 0', ' ')
    today = et.strftime('%Y-%m-%d')

    spy_now = yahoo('SPY')['price']

    still_open = []
    for p in ledger['positions']:
        if p.get('status') == 'closed':
            ledger['closed'].append(p); continue
        try:
            y = yahoo(p['ticker'])
        except Exception as e:
            print(f"WARN {p['ticker']}: {e}", file=sys.stderr)
            still_open.append(p); continue
        p['current_price'] = y['price']
        p['spark'] = [c for _, c in y['closes']][-45:]
        if p.get('flag_price') is None:
            p['flag_price'] = y['price']; p['flag_date'] = today
            p['spy_flag_price'] = spy_now
            p.setdefault('stop_pct', -25); p.setdefault('status', 'open')
            p['stake'] = band_stake(p)
        stake = p.get('stake') or 0
        interp_targets(p)
        p['shares'] = round(stake / p['flag_price'], 6) if stake else 0
        p['value'] = round(p['shares'] * p['current_price'], 2)
        p['pl'] = round(p['value'] - stake, 2)
        p['pl_pct'] = round(100 * (p['current_price'] / p['flag_price'] - 1), 2)
        spy_ret = 100 * (spy_now / p['spy_flag_price'] - 1) if p.get('spy_flag_price') else 0.0
        p['spy_ret_pct'] = round(spy_ret, 2)
        p['alpha_pct'] = round(p['pl_pct'] - spy_ret, 2)
        p['stop_price'] = round(p['flag_price'] * (1 + p['stop_pct'] / 100), 2)
        p['day_pct'] = (round(100 * (p['current_price'] / p['spark'][-2] - 1), 2)
                        if len(p.get('spark', [])) > 1 and p['spark'][-2] else 0.0)
        p['vs_ref_pct'] = (round(100 * (p['current_price'] / p['ref_price'] - 1), 1)
                           if p.get('ref_price') else None)
        p['review_due'] = today >= p.get('review_date', '9999-12-31')
        # vol24 direction evaluation
        if p.get('direction') and not p.get('direction_outcome') and today >= p.get('eval_date', '9999'):
            move = p['pl_pct']
            hit = (move > 0) if p['direction'] == 'bull' else (move < 0)
            p['direction_outcome'] = 'hit' if hit else 'miss'
            p['status'] = 'closed'; p['exit_price'] = p['current_price']
            p['exit_date'] = today; p['exit_reason'] = f"5-day eval: {p['direction']} {'✓' if hit else '✗'} ({move:+.1f}%)"
            cash[p.get('book','core')] = round(cash.get(p.get('book','core'),0) + p['value'], 2)
            ledger['closed'].append(p); time.sleep(0.3); continue
        # stop enforcement
        if p['current_price'] <= p['stop_price']:
            p['status'] = 'closed'; p['exit_price'] = p['current_price']
            p['exit_date'] = today; p['exit_reason'] = f"stop {p['stop_pct']}%"
            cash[p.get('book','core')] = round(cash.get(p.get('book','core'),0) + p['value'], 2)
            ledger['closed'].append(p)
        else:
            still_open.append(p)
        time.sleep(0.3)
    ledger['positions'] = still_open

    total_val = sum(p.get('value', 0) for p in still_open)
    closed_pl = sum(p.get('pl', 0) for p in ledger['closed'])
    invested = round(sum(p.get('stake', 0) for p in still_open), 2)
    total_cash = round(sum(cash.values()), 2)
    spy_val = sum((p.get('stake', 0)) * (spy_now / p['spy_flag_price']) if p.get('spy_flag_price') else p.get('stake', 0)
                  for p in still_open)
    opt_stats = update_options(ledger, today)
    try:
        import option_score as _OS
        conf_grade = _OS.grade_bands(ledger.get('closed_options', []))
        _v, _m = _OS.kill_check(ledger.get('closed_options', []))
        conf_kill = {'verdict': _v, 'msg': _m}
    except Exception as _e:
        conf_grade, conf_kill = {}, {'verdict': 'pending', 'msg': f'score module unavailable: {_e}'}
    calls = [p for p in still_open + ledger['closed'] if p.get('direction')]
    resolved = [p for p in calls if p.get('direction_outcome')]
    hits = sum(1 for p in resolved if p['direction_outcome'] == 'hit')
    snap = {'date': today, 'value': round(total_val + total_cash, 2), 'spy_value': round(spy_val + total_cash, 2),
            'invested': invested, 'n_open': len(still_open), 'n_closed': len(ledger['closed'])}
    ledger['history'] = [h for h in ledger['history'] if h['date'] != today] + [snap]
    ledger['meta']['last_scan'] = stamp
    json.dump(ledger, open(ledger_path, 'w'), indent=1)

    data = json.dumps({'positions': still_open, 'closed': ledger['closed'],
                       'history': ledger['history'], 'meta': ledger['meta'],
                       'watchlist': ledger.get('watchlist', []),
                       'invested': invested, 'total_val': total_val, 'closed_pl': closed_pl,
                       'cash': cash, 'total_cash': total_cash, 'portfolio': port,
                       'signals': ledger['meta'].get('signals', []),
                       'spy': ledger.get('spy_state', {}), 'spy_calls': ledger.get('spy_calls', []),
                       'whale': ledger['meta'].get('whale', {}), 'whale_events': ledger.get('whale_events', []),
                       'spy_val': round(spy_val, 2), 'stamp': stamp,
                       'options': ledger['options_positions'], 'closed_options': ledger['closed_options'],
                       'opt_stats': opt_stats,
                       'conf_grade': conf_grade, 'conf_kill': conf_kill,
                       'spy_cand': ledger.get('spy_entry_candidate'),
                       'dir_stats': {'n': len(resolved), 'hits': hits, 'open_calls': len(calls) - len(resolved)}})
    html = HTML.replace('__DATA__', data)
    html = html.replace('__FINNHUB_KEY__', ledger['meta'].get('finnhub_key', ''))
    open(out_path, 'w').write(html)
    print(f"OK open={len(still_open)} closed={len(ledger['closed'])} value=${total_val:,.2f} "
          f"calls={len(calls)} resolved={len(resolved)}")

HTML = r'''<!DOCTYPE html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scanner Terminal</title>
<link rel="icon" type="image/png" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAACDElEQVR42u2bvW3DMBCFScKtNkgfF7I2yAaqs4MzggbwCM4OrrOBN3BcyL020ABKYSigCZKixB/c8XidIcHA9+7u3dGWOHOIqqomhjDGceRL9/AcwdcIwXMGdxFCUIE3sQkq8CZGQQlexyoY8RDUsq9WAfkK4BSzbx2DRYBMou56Vnf94n27HEBJV8Dv27vx2v20p9ECOlAX+GKCuQig+oBr9tGa4HR+ri6H4cHY8OoFh+FBowJkUNkIbaaYjQAmeBIeIPe7Lzw6AULDoxIgBjzKFlgz4tAKMI+5UPs+6gqQ4UNnH1ULxIAHL0DM0ge3Ctv6Plb2UbRAyJGHRoB51Y0ND1KAtac5NfgXh+cB16ZhH7dbsEOODDmdp9XQ4FsgRemDE2Br6ftkn7GI/wxdm8Z6XW6JpYOOLySaFkhZ+mAESLHtgRXA5Ywfs/y9xqDraNPFsb2wun1dczkLN9rAngWO7SXJfg9OABn8++fzOQWAGO9ubdmbPuvaQQcOLYJXwGxsRw/wVP0fVAB1nEHNeBQBtOZm2QS3Tg9wAsjgqrPLkD4jE6wAqsFBBQwuwBZnhyyOswB11/87OyaTCyKA7PA6cKzlv/h7gO3RE8jGFuw0OAPfT3swu3vSCqAQ5TE5l1fLco1xHHmpgFkJitkvHiALQKkKZFZhukABXtsCOYugYysvT7t8Qc6vz/8B7+zaiU8cB7EAAAAASUVORK5CYII=">
<link rel="apple-touch-icon" sizes="180x180" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAAAE10lEQVR42u3dvXHUQBiA4V0N6XVADgG4A3dATA+4BAqgBOiBmA7owBBATgcq4Ihuxhj7TjpJq91vnzdjxn/gZz4+/d3ltGOHw+GYFK5xHPNe3zvDq0jIM8SKhDuDrEiwM8iKBDuDrEiwB5hVS2tYGmBWJNQZZEVaQQaYFWlaDzArEuoBZkVCPcCsSKgHmBUJ9eCfSZEaTGdFmtIDzIqEeoBZkVDboRV7hzad1fKUNqEVd0Kbzmp9SpvQijmhTWdFmNImtOLu0FII0NYNRVk7TGhZOSSgJaClmaAdEGrr3nz8VezA0IRWEcylUGcTWiWn8c9Przf9/i/8ClRqjdgaM9Aqtv+WwJySsxy60I+Xr5r6eYHW5qhLTWegVSVKoCWgVWJKl57sQCvU6gG0NkO9B36gNbtSl7GBVteYgdZizM+tFXvt2kAr1AGiu+2UUkrp+Pk8g7d/fv/z54dXD/Nd/m96m9BqBvNTn/8Q8J6TG2iYZ2N+6t6Ox6iBVnVNxVzTXg20VsNcQ0ArDGagFQoz0AqFGWiFwgy0QmEGWqEwA915T91s1DJmoGEOhTklLzQDciDMJjTMoTADDXO4gO68SNMZ6M6ncyuvhgS0uls1gO4cc8TpDHSnRcUMdAMdPx8vPibV+94MtL0ZaNmbgZa9GWitPZ17wQy0vRlo2ZuBFsxAy0Eg0KZzx5iBdhAYLo9gVdCUS9sPP+bcy9v2PJ1N6EYw25uBDov5uekMM9DNdemV9AV0CMzRngsEuuNgBjrMdF4Dc77Lq/yMa30doO3Nu2OsBXNKzkPbmy+gPJ2JqQmtCW1vtkMr7t4MdMd9v7lJ329uwu3NQCvc3gy0YAZaDgLL5bSdg8CztXK6zoRuZNUQ0PZmoGVvBlpB92YHhY035cLJcx9ze39/1ff0kKsJvRvmtT//HGbTGejdMK/9dWAGurl6ePNLO3THkAV0OMims5Wjecyn19GA2YQOMZXffPwFM9Ax9uTTdM4pr/JSYK3dQLRl+XA4HHv9y691uu3Du6+zID9uCWqYTehVW4r5EsrWnroGumPI6gz0aSW49n4JkIEWyECDDDPQgTB/+fa+qrUIaF0NWUCDLKBB1pQGmGGOVBWXvpdcgt76mb6pkB0UWjkWYz59/hxMa0MW0KthnoMaZDt0mGA2oUEW0CAL6B0wn+65uF1pj3eGA+jdpvLjG4hu7+93OW0ooJ9tysWRc3fCnUNZ4z3ZCgp6KWQB3QxkmIE2lQW0qSygQVaPoKdgPl0cufW7VK2ga7pH2ek6oENAFtDF1gupWtCmssKANpUVAvSUG4nmQHbgpl1Ab3GfMsx6WLGnvufcp3zKnW6qbkJfe5+ydE2bPyR7CerPT69hVtsHhSaymp3QT+GFWSEmNMgKMaFhVqm6fp9CBZzQ4zh6AzyFaBzH7M3rZYeWgJaAlq4A7cBQEQ4ITWhZOaQmQFs71Pq6YUIr9sphSqvl6WxCK/5BoSmtVqezCa34E9qUVqvT+eyEhlqtYb64ckCtljDbodXHDm1Kq8XpPHlCQ60WMM9aOaBW7Zhn79BQq2bMVx0UQq1aMaeU0iKcXtNDtUC+ekKb1qoV82LQUKsmzItXDiuIaoG8CWiwtff/7puuC2Cr9JpabP+FG+IS32fXAzrI4V27vyNXSq+UiC+oAAAAAElFTkSuQmCC">
<meta name="theme-color" content="#0d0d0d">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Scanner">
<link rel="manifest" href='data:application/manifest+json,{&quot;name&quot;: &quot;Scanner Terminal&quot;, &quot;short_name&quot;: &quot;Scanner&quot;, &quot;display&quot;: &quot;standalone&quot;, &quot;background_color&quot;: &quot;#0d0d0d&quot;, &quot;theme_color&quot;: &quot;#0d0d0d&quot;, &quot;icons&quot;: [{&quot;src&quot;: &quot;data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAYAAABS3GwHAAAFAUlEQVR42u3dzU0bURRA4TejbN0Be7wgdJAOsqaHUAIFUAL0kDUdpAPwguzTwRTgLKJEDsI28//eu99ZRUpEJOucd+/YnqFJK7PZbPYJYem6rlnz/28Ij8hBNKRH5Bga4iNyCA3xETmEhviIHEJDfEQOoSU/SmUK91ryI3IEDfEReSVqyY/I06AlPyJH0JIfkSNoyY/IEbReKkSmnaokoMQp0JIfkSNoyY/IEbgGgGsApz+iTgETACaA0x9Rp4AJABPA6Y+oU8AEgAng9EfUKWACwAQAQgdg/UHUNcgEgAkACAAISGP/hwkAZMDV3asJAPKnlNLufrvI//vJS48op70JgKJEX2IKmADI8jRfagVyEYxR7B/26eXiskj5BYDJmCqCJeUXAIo9uQUAAQkAJBYAKohgrXAEgNCTQAAIjQAwC30+HFtzWggAq8pvAoD8AkDt8h9bc9a+WPZlOHyI/cPpLw1//vXz6N+9XFym9JDS7nab3YQwATC//Ac/5/DEz+GtUgFgEfmPRSAAFEtf+XPa/QWA1eR3DQDyCwDkFwDILwCQXwAgvwBAfgEgE059baEG+QWA0PILAKHlFwBCyy8AhJZfAAgtvwAQHgEg7OkvAAy6j1cAqF5+KxBCE+H0T8lTIYrl8F7d5rax+pgAIL8AYO8XAOz9AoDVRwAgvwDIb+8XAJz+AnD6k18A5Ce/AOz9EIC9H74LlDnnns//3r859Swf8psAVcn/FvILgPwQAJz+AghE7c/wFADILwCQXwBY8KJ3yO2WS/48AWD2038qaXOTPyUfhFl9Rso79uZ8EwD2fgEgp70/ElagHvy4vv735y/Pz1Xv/SYArD4CAPkFAHu/awDkydqnf4lvfZoAVh8IgPwQgL1fALD3CwAZEfmX1i1+Eb/ZbPZehv85/MR3CGM+JZ5L/tLfrTEBCpF/zM/wMCsBFC//HD/L6iMAez9mwSfBGYtPfgGEFR9WoPDyO/1NgLCnPvkFEHbd2d1vU5OaQc8HfQ+fAQigGPHfijs2AvILYHG+ff0+SvyPCFz640hcBJP/rPwwAYgPAeT8OBLiC8CJT3wBkJ/4AiA++QVA/Menmz/XK146AUQUHwIgPorAB2HkD01WN8XnejP6UPFzfIQ6Ml2BproZvY90Q25KceILIEv5+0RAfIS9CJ5j1YEAqhOf/AIgPgQQSf7d/Xay6xHvAAmgKPEPxV3z7VgIYDB9P8R6K/85gXP/NakIGsBU4kMA1ctPfFQRgFMfIQMgPsIGYN1ByACIj5ABDFl3Hp9u3IqI8gPI8cYU7/0LIKT4EEC26w5QdABXd6/pG/ERMQA3pyBkAL6jj5ABLCG+d2qQZQBLrDvkR19mfy7Q2N+TlZLv4WM+Zn8yXJ+vJfgKA0JcBBMfYSbAKcF391vyo/4AnPoIH8Bf4Z36CDsBiI/cyOrx6ECYawAgiwC6rmu8DIhI13WNCQArECAAIGoArgMQcf83AWACeAkgAGsQAq4/JgBMgGNlALWf/iYATIBzhQC1nv4mAEyAj5YC1Hb6mwAwAfoWA9Ry+p+dACJAzfJ/aAUSAWqV3zUAXANMVRJQ2unfawKIALXJ33sFEgFqkn/QNYAIUIv8KaU0SmYP1UKp4g+eAKYBapF/dAAiQMnyj16BrEQoVfxZAhACShF/1gCEgNzFXyQAISD368vFL2DFgLWlXzUAQWBN4d/yG87pYmf8jLwZAAAAAElFTkSuQmCC&quot;, &quot;sizes&quot;: &quot;192x192&quot;, &quot;type&quot;: &quot;image/png&quot;}, {&quot;src&quot;: &quot;data:image/png;base64,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&quot;, &quot;sizes&quot;: &quot;512x512&quot;, &quot;type&quot;: &quot;image/png&quot;}]}'>
<style>
:root[data-theme="dark"]{color-scheme:dark;
 --surface:#111511;--page:#070907;--ink:#f2fff2;--ink2:#b7c9b7;--muted:#7d8f7d;
 --grid:#1c241c;--axis:#2a382a;--border:rgba(84,240,120,.14);
 --s1:#54f078;--up:#54f078;--down:#ff5252;--warn:#ffd23e;}
:root[data-theme="light"]{color-scheme:light;
 --surface:#fcfcfb;--page:#f9f9f7;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
 --grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);
 --s1:#2a78d6;--up:#006300;--down:#d03b3b;--warn:#c98500;}
*{box-sizing:border-box;margin:0}
body{font:13px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--page);color:var(--ink);padding:14px 16px 34px}
h1{font-size:16px;font-weight:700;letter-spacing:.04em}h1 b{color:var(--s1)}
.sub{color:var(--muted);font-size:11.5px;margin-top:2px}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:10px;flex-wrap:wrap}
.toggle{border:1px solid var(--border);background:var(--surface);color:var(--ink2);border-radius:7px;padding:4px 10px;cursor:pointer;font-size:12px}
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--axis);margin-bottom:12px;flex-wrap:wrap}
.tab{padding:7px 14px;font-size:12.5px;color:var(--ink2);cursor:pointer;border:none;background:none;border-bottom:2px solid transparent}
.tab.on{color:var(--s1);border-bottom-color:var(--s1);font-weight:650}
.tab .n{color:var(--muted);font-size:11px;margin-left:4px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(138px,1fr));gap:8px;margin-bottom:12px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:9px 12px}
.tile .lbl{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.tile .val{font-size:19px;font-weight:650;margin-top:2px;font-variant-numeric:tabular-nums}
.tile .sm{font-size:11.5px;color:var(--ink2);margin-top:1px}
.pos{color:var(--up)}.neg{color:var(--down)}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px 14px;margin-bottom:12px}
.panel h2{font-size:11.5px;font-weight:650;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
.signals{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:8px}
.sig{border:1px solid var(--border);border-radius:7px;padding:8px 10px;font-size:12px}
.sig b{display:block;margin-bottom:2px;font-size:12px}
.sig.flow b{display:inline;margin:0}
.sig.flow span{line-height:1.6}
.sig span{color:var(--ink2)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;text-align:right;padding:6px 7px;border-bottom:1px solid var(--axis);font-weight:600;cursor:pointer;user-select:none;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
td{padding:6px 7px;border-bottom:1px solid var(--grid);text-align:right;font-size:12.5px;white-space:nowrap}
tr.thesis td{font-size:11.5px;color:var(--ink2);text-align:left;padding:2px 7px 8px;border-bottom:1px solid var(--axis);white-space:normal}
tr.main:hover td{background:color-mix(in srgb,var(--s1) 8%,transparent)}
.tk{font-weight:650;font-size:13px}
.nm{color:var(--muted);font-size:10.5px}
.tag{display:inline-block;font-size:9.5px;border:1px solid var(--border);border-radius:12px;padding:0 6px;color:var(--ink2);margin-left:5px;vertical-align:1px}
.badge{display:inline-block;min-width:30px;text-align:center;font-size:11.5px;font-weight:650;border-radius:6px;padding:1px 6px;border:1px solid var(--border)}
.dir{font-size:10px;font-weight:700;border-radius:5px;padding:1px 6px;letter-spacing:.03em}
.dir.bull{color:var(--up);border:1px solid var(--up)}
.dir.bear{color:var(--down);border:1px solid var(--down)}
.review{font-size:9.5px;color:var(--warn);border:1px solid var(--warn);border-radius:12px;padding:0 5px;margin-left:4px;vertical-align:1px}
.tgt{font-size:11px;color:var(--ink2)}
.tgt b{color:var(--ink)}
.chartwrap{position:relative}
svg text{font:10px system-ui,sans-serif;fill:var(--muted)}
.tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--border);border-radius:7px;padding:5px 8px;font-size:11.5px;box-shadow:0 2px 10px rgba(0,0,0,.3);display:none;z-index:5;white-space:nowrap}
.legend{display:flex;gap:14px;font-size:11.5px;color:var(--ink2);margin-bottom:5px}
.legend i{display:inline-block;width:13px;height:3px;border-radius:2px;vertical-align:middle;margin-right:4px}
.foot{color:var(--muted);font-size:11px;margin-top:12px;line-height:1.55}
.watch li{margin:2px 0 2px 15px;font-size:12px;color:var(--ink2)}
.empty{color:var(--muted);font-size:12px;padding:6px 0}
.ocard{border:1px solid var(--border);border-radius:9px;padding:11px 12px;margin-bottom:9px;background:rgba(255,255,255,.015)}
.ohead{display:flex;gap:13px;align-items:flex-start}
.oconf{flex:0 0 78px;border:2px solid;border-radius:9px;padding:6px 4px 5px;text-align:center}
.oconfn{font-size:31px;font-weight:800;line-height:1.03;font-variant-numeric:tabular-nums}
.oconfb{display:block;font-size:8.5px;font-weight:800;letter-spacing:.07em;color:#07090c;border-radius:3px;padding:1px 0;margin:3px 3px 0}
.oconfl{font-size:8.5px;color:var(--muted);letter-spacing:.06em;text-transform:uppercase;margin-top:3px}
.otitle{display:flex;align-items:baseline;gap:7px;flex-wrap:wrap;margin-bottom:7px}
.obig{font-size:19px;font-weight:750;letter-spacing:-.01em}
.okeys{display:grid;grid-template-columns:repeat(auto-fit,minmax(88px,1fr));gap:6px}
.okey{background:var(--grid);border-radius:6px;padding:4px 7px 5px}
.okey span{display:block;font-size:8.5px;color:var(--muted);letter-spacing:.06em;text-transform:uppercase}
.okey b{font-size:14px;font-weight:700;font-variant-numeric:tabular-nums}
.obar{margin-top:6px;font-size:11px;color:var(--muted);display:flex;gap:6px;flex-wrap:wrap}
.odet{margin-top:9px;border-top:1px solid var(--border);padding-top:7px}
.odet summary{cursor:pointer;font-size:11px;color:var(--ink2);list-style:none;user-select:none}
.odet summary::-webkit-details-marker{display:none}
.odet summary::before{content:'▸ ';color:var(--muted)}
.odet[open] summary::before{content:'▾ '}
.odl{display:grid;grid-template-columns:repeat(auto-fit,minmax(235px,1fr));gap:2px 22px;margin-top:9px;font-size:11.5px}
.odl>div{display:flex;justify-content:space-between;gap:14px;border-bottom:1px dotted var(--border);padding:3px 0}
.odl span{color:var(--muted);white-space:nowrap}
@media(max-width:620px){.oconf{flex:0 0 64px}.oconfn{font-size:25px}.obig{font-size:16px}}
.tstrip{display:flex;align-items:flex-start;gap:9px;border:1px solid;border-radius:7px;padding:6px 9px;margin-top:9px;background:rgba(255,255,255,.02)}
.tbadge{flex:0 0 auto;font-size:9.5px;font-weight:800;letter-spacing:.06em;color:#07090c;border-radius:4px;padding:2px 7px;white-space:nowrap}
.ttext{font-size:11.5px;color:var(--ink2);line-height:1.45}
.tbar{display:flex;align-items:center;gap:11px;flex-wrap:wrap;margin-bottom:11px}
.tbtn{font-size:12px;font-weight:650;padding:6px 12px}
.tbar .nm{flex:1;min-width:220px;font-size:11px;line-height:1.45}
.fresh{border:2px solid;border-radius:11px;padding:13px 14px}
.dteWrap{border:2px solid var(--s1);border-radius:11px;padding:14px 15px 13px;background:linear-gradient(180deg,rgba(84,240,120,.07),transparent 65%)}
.dteLbl{font-size:9.5px;font-weight:800;letter-spacing:.13em;color:var(--muted)}
.dteStrike{font-size:40px;font-weight:800;letter-spacing:-.025em;line-height:1.08;margin:1px 0 3px;font-variant-numeric:tabular-nums}
.dteSub{font-size:11.5px;color:var(--muted);margin-bottom:12px}
.dteGrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));gap:7px}
.dbox{background:var(--grid);border:1px solid var(--border);border-radius:7px;padding:6px 9px 7px}
.dbox span{display:block;font-size:8.5px;color:var(--muted);letter-spacing:.07em;text-transform:uppercase;margin-bottom:1px}
.dbox b{font-size:17px;font-weight:750;font-variant-numeric:tabular-nums}
@media(max-width:620px){.dteStrike{font-size:30px}.dbox b{font-size:15px}}
.chip{display:inline-block;font-size:9.5px;font-weight:700;letter-spacing:.05em;border-radius:4px;padding:1px 6px;margin-left:6px;vertical-align:1px}
.chip.bullish{color:var(--up);border:1px solid var(--up)}
.chip.bearish{color:var(--down);border:1px solid var(--down)}
.chip.neutral{color:var(--muted);border:1px solid var(--muted)}
.sigcards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:9px}
.scard{border:1px solid var(--border);border-radius:8px;padding:10px 12px;background:color-mix(in srgb,var(--s1) 3%,transparent)}
.scard .dir{font-size:14px;font-weight:800;letter-spacing:.06em}
.scard table{margin-top:6px}
.scard td{padding:2px 0;border:none;font-size:12px}
.scard td:first-child{color:var(--muted);text-align:left}
.pipe{display:flex;gap:6px;flex-wrap:wrap;align-items:center;font-size:11px;color:var(--ink2)}
.pipe b{color:var(--s1)}
.pipe .arr{color:var(--muted)}
.view{display:none}.view.on{display:block}
</style></head><body>
<header>
 <div><h1>SCANNER <b>TERMINAL</b></h1><div class="sub" id="stamp"></div></div>
 <div style="display:flex;gap:6px;align-items:center">
  <button class="toggle" id="rescan" onclick="manualRefresh()" style="border-color:var(--s1);color:var(--s1)">⟳ Refresh prices</button>
  <button class="toggle" onclick="flip()">Light / dark</button></div>
</header>
<div class="tabs" id="tabs"></div>
<div id="views"></div>
<div class="foot" id="foot"></div>
<script>
const D=__DATA__;
const fmt$=v=>(v<0?'-$':'$')+Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const fmtP=v=>(v>0?'+':'')+v.toFixed(2)+'%';
const cls=v=>v>0.001?'pos':(v<-0.001?'neg':'');
const css=v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim();
function flip(){const r=document.documentElement;r.setAttribute('data-theme',r.getAttribute('data-theme')==='dark'?'light':'dark');render();}
const BOOKS=[['overview','Overview'],['spy','SPY / 1DTE'],['whale','Whale Watch'],['core','Core'],['vol24','24h Volatility'],['options','Options'],['penny','Penny'],['longterm','Long-term'],['journal','Journal'],['closed','Closed']];
let tab='overview', sortKey='score_total', sortDir=-1;
const by=b=>D.positions.filter(p=>(p.book||'core')===b);

function setStamp(){document.getElementById('stamp').textContent='Last scan '+D.stamp+' · '+D.positions.length+' open / '+D.closed.length+' closed · $1,000 paper portfolio · weekday auto-scan'+(liveStamp?' · LIVE quotes '+liveStamp:'');}

function scoreBadge(p){
 const s=p.score_total,col=s>=70?css('--up'):(s<50?css('--warn'):css('--muted'));
 const t=p.score?`catalyst ${p.score.catalyst}/30 · momentum ${p.score.momentum}/25 · liquidity ${p.score.liquidity}/15 · risk ${p.score.risk}/15 · valuation ${p.score.valuation}/15`:'';
 return`<span class="badge" title="${t}" style="color:${col};border-color:${col}">${s}</span>`;
}
function spark(p,w,h){
 const s=p.spark||[];if(s.length<2)return'';
 const mn=Math.min(...s),mx=Math.max(...s),rg=(mx-mn)||1;
 const pts=s.map((v,i)=>`${(i/(s.length-1)*w).toFixed(1)},${(h-2-(v-mn)/rg*(h-4)).toFixed(1)}`).join(' ');
 return`<svg width="${w}" height="${h}"><polyline points="${pts}" fill="none" stroke="${css(s[s.length-1]>=s[0]?'--up':'--down')}" stroke-width="1.5"/></svg>`;
}
function tgtCell(p,h){
 const t=p.targets&&p.targets[h];if(!t)return'<span class="tgt" style="color:var(--muted)">—</span>';
 return`<span class="tgt" title="bull ${fmt$(t.bull)} / base ${fmt$(t.base)} / bear ${fmt$(t.bear)}"><b>${fmt$(t.base)}</b><br><span style="color:var(--muted)">${fmt$(t.bear)}–${fmt$(t.bull)}</span></span>`;
}
function dirCell(p){
 if(!p.direction)return'—';
 return`<span class="dir ${p.direction}">${p.direction.toUpperCase()}</span>`+(p.direction_outcome?` ${p.direction_outcome==='hit'?'✓':'✗'}`:` <span class="nm">eval ${p.eval_date}</span>`);
}
function posTable(list,opts){
 if(!list.length)return'<div class="empty">No open positions in this book.</div>';
 const showDir=opts.dir,showTgt=opts.tgt;
 const cols=[['Position',null],['Score','score_total']]
  .concat(showDir?[['Call',null]]:[])
  .concat([['Stake','stake'],['Flag','flag_price'],['Now','current_price'],['Day','day_pct'],['P/L %','pl_pct'],['Alpha','alpha_pct']])
  .concat(showTgt?[['3m tgt',null],['6m tgt',null],['12m tgt',null]]:[])
  .concat([['Stop','stop_price'],['3 mo',null]]);
 const rows=[...list].sort((a,b)=>{if(!sortKey)return 0;const av=a[sortKey],bv=b[sortKey];return(typeof av==='string'?String(av).localeCompare(bv):av-bv)*sortDir;})
  .map(p=>`<tr class="main">
   <td><span class="tk">${p.ticker}</span><span class="tag">${p.news_tag}</span>${p.review_due?'<span class="review">review</span>':''}<br><span class="nm">${p.name}</span></td>
   <td>${scoreBadge(p)}</td>
   ${showDir?`<td>${dirCell(p)}</td>`:''}
   <td>${fmt$(p.stake||0)}</td><td>${fmt$(p.flag_price)}</td><td>${fmt$(p.current_price)}</td>
   <td class="${cls(p.day_pct)}">${fmtP(p.day_pct)}</td>
   <td class="${cls(p.pl_pct)}">${fmtP(p.pl_pct)}</td>
   <td class="${cls(p.alpha_pct)}"><b>${fmtP(p.alpha_pct)}</b></td>
   ${showTgt?`<td>${tgtCell(p,'t3')}</td><td>${tgtCell(p,'t6')}</td><td>${tgtCell(p,'t12i')}</td>`:''}
   <td style="color:var(--muted)">${fmt$(p.stop_price)}</td>
   <td>${spark(p,90,26)}</td></tr>
  <tr class="thesis"><td colspan="${cols.length}">${p.thesis}${p.ref_label?` <span class="nm">(ref: ${p.ref_label}${p.vs_ref_pct!=null?', '+fmtP(p.vs_ref_pct):''})</span>`:''}</td></tr>`).join('');
 return`<div style="overflow-x:auto"><table class="sortable"><thead><tr>${cols.map(([n,k])=>`<th data-k="${k||''}">${n}${k===sortKey?(sortDir<0?' ▼':' ▲'):''}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`;
}
function curveSvg(){
 const H=D.history;
 if(H.length<2)return'<div class="empty">Equity curve builds as daily scans accumulate — first point '+(H[0]?H[0].date:'today')+'. Divergence between the two lines = alpha.</div>';
 const W=Math.min(document.body.clientWidth-60,1100),Hh=190,padL=50,padB=18,padT=8;
 const vals=H.flatMap(h=>[h.value,h.spy_value]);
 const mn=Math.min(...vals)*0.998,mx=Math.max(...vals)*1.002;
 const x=i=>padL+i/(H.length-1)*(W-padL-8), y=v=>padT+(mx-v)/(mx-mn)*(Hh-padT-padB);
 let s=`<svg width="${W}" height="${Hh}">`;
 [mn,(mn+mx)/2,mx].forEach(g=>{s+=`<line x1="${padL}" x2="${W-4}" y1="${y(g)}" y2="${y(g)}" stroke="${css('--grid')}"/><text x="${padL-5}" y="${y(g)+3}" text-anchor="end">$${Math.round(g)}</text>`;});
 s+=`<polyline points="${H.map((h,i)=>x(i)+','+y(h.spy_value)).join(' ')}" fill="none" stroke="${css('--muted')}" stroke-width="2"/>`;
 s+=`<polyline points="${H.map((h,i)=>x(i)+','+y(h.value)).join(' ')}" fill="none" stroke="${css('--s1')}" stroke-width="2"/>`;
 H.forEach((h,i)=>{if(i%Math.ceil(H.length/8)===0||i===H.length-1)s+=`<text x="${x(i)}" y="${Hh-3}" text-anchor="middle">${h.date.slice(5)}</text>`;});
 return s+'</svg>';
}
function alphaBars(){
 const items=D.positions.map(p=>({t:p.ticker,v:p.alpha_pct})).sort((a,b)=>b.v-a.v);
 if(!items.length)return'<div class="empty">No open positions.</div>';
 const W=Math.min(document.body.clientWidth-60,1100),H=170,padL=40,padB=20,padT=10;
 const mx=Math.max(...items.map(i=>i.v),2),mn=Math.min(...items.map(i=>i.v),-2);
 const y=v=>padT+(mx-v)/(mx-mn)*(H-padT-padB);
 const bw=Math.max(10,Math.min(38,(W-padL)/items.length-6));
 let s=`<svg width="${W}" height="${H}">`;
 s+=`<line x1="${padL}" x2="${W-4}" y1="${y(0)}" y2="${y(0)}" stroke="${css('--axis')}"/><text x="${padL-5}" y="${y(0)+3}" text-anchor="end">0%</text>`;
 items.forEach((it,i)=>{
  const x=padL+8+i*((W-padL-8)/items.length);
  const y0=y(Math.max(it.v,0)),y1=y(Math.min(it.v,0));
  s+=`<rect x="${x}" y="${y0}" width="${bw}" height="${Math.max(Math.abs(y1-y0),2)}" rx="3" fill="${it.v>=0?css('--up'):css('--down')}"><title>${it.t} alpha ${fmtP(it.v)}</title></rect>`;
  s+=`<text x="${x+bw/2}" y="${H-5}" text-anchor="middle">${it.t}</text>`;
 });
 return s+'</svg>';
}
const SIGNALS=[
 ['Venezuela oil fallout','Blockade aftermath keeps heavy crude tight; KOS is the high-beta expression.'],
 ['AI buildout, phase 2','SKHY (memory), CRWV (GPU cloud), TSM (fab) up the stack; power crunch feeds STDN and OKLO.'],
 ['Record IPO window','$251B raised 2026; biotech leads cohort. Anthropic, OpenAI, Strava filings on watch.'],
 ['Earnings vol harvest','NIQ +42% / ONON −20% / FRMI +21% — the 24h book calls direction and gets graded in 5 days.'],
 ['Crypto crosscurrents','Options positioned for August BTC dip vs strong equity tape — IOND, CRCL test it.'],
];
function overview(){
 const totPL=D.total_val-D.invested+D.closed_pl;
 const alphaAvg=D.positions.length?D.positions.reduce((s,p)=>s+p.alpha_pct,0)/D.positions.length:0;
 const ds=D.dir_stats;
 const hitTxt=ds.n?`${ds.hits}/${ds.n} (${Math.round(100*ds.hits/ds.n)}%)`:'0 resolved';
 const cap=(D.portfolio&&D.portfolio.capital)||1000;
 const pv=D.total_val+D.total_cash;
 const kpis=[
  ['Portfolio value',fmt$(pv),'started $'+cap.toLocaleString()+' · '+D.positions.length+' pos'],
  ['Invested / cash',fmt$(D.invested),fmt$(D.total_cash)+' cash'],
  ['Total P/L',`<span class="${cls(pv-cap)}">${fmt$(pv-cap)}</span>`,`<span class="${cls(pv-cap)}">${fmtP(100*(pv-cap)/cap)}</span>`],
  ['Avg alpha vs SPY',`<span class="${cls(alphaAvg)}">${fmtP(alphaAvg)}</span>`,'per open position'],
  ['Direction hit rate',hitTxt,ds.open_calls+' call(s) pending'],
  ['Options book',`<span class="${cls(D.opt_stats.total_pl)}">${fmt$(D.opt_stats.total_pl)}</span>`,fmt$(D.opt_stats.spent)+' premium at risk'],
 ].map(([l,v,s])=>`<div class="tile"><div class="lbl">${l}</div><div class="val">${v}</div><div class="sm">${s}</div></div>`).join('');
 const alloc=(D.portfolio&&D.portfolio.allocations)||{};
 const bookRows=['core','vol24','penny','longterm'].map(b=>{
  const l=by(b);if(!l.length&&!alloc[b])return'';
  const inv=l.reduce((s2,p)=>s2+(p.stake||0),0), val=l.reduce((s2,p)=>s2+p.value,0);
  const csh=(D.cash&&D.cash[b])||0, pl=val-inv, wt=100*(val+csh)/pv;
  return`<tr><td style="text-align:left">${{core:'Core',vol24:'24h Volatility',penny:'Penny',longterm:'Long-term'}[b]}</td><td>${l.length}</td><td>${fmt$(alloc[b]||0)}</td><td>${fmt$(inv)}</td><td>${fmt$(csh)}</td><td>${fmt$(val+csh)}</td><td class="${cls(pl)}">${fmt$(pl)}</td><td>${wt.toFixed(1)}%</td></tr>`;
 }).join('');
 const holdings=[...D.positions].sort((a,b)=>b.value-a.value).slice(0,8).map(p=>
  `<tr><td style="text-align:left"><span class="tk">${p.ticker}</span> <span class="tag">${p.book}</span></td><td>${fmt$(p.stake||0)}</td><td>${fmt$(p.value)}</td><td class="${cls(p.pl)}">${fmt$(p.pl)}</td><td>${(100*p.value/pv).toFixed(1)}%</td></tr>`).join('');
 const sigs=(D.signals&&D.signals.length)?D.signals:SIGNALS.map(([t,b])=>({title:t,body:b,sentiment:'neutral',sector:''}));
 function timingStock(p){
  const dir=p.direction, pr=p.current_price, e=p.flag_price, st=p.stop_price;
  const r=100*(pr/e-1), adverse=dir==='bull'?r:-r;
  if((dir==='bull'&&pr<=st)||(dir==='bear'&&pr>=e*(1-p.stop_pct/100)*1.0&&false))return['INVALIDATED','neg'];
  if(dir==='bull'&&pr<=st)return['INVALIDATED','neg'];
  if(dir==='bear'&&r>=-p.stop_pct)return['INVALIDATED','neg'];
  if(adverse<=-1)return['DISCOUNT — below entry','pos'];
  if(adverse<=1.5)return['OPTIMAL — near entry','pos'];
  if(adverse<=4)return['EXTENDED — chasing','warn'];
  return['TOO LATE — moved '+(adverse>0?'+':'')+adverse.toFixed(1)+'%','neg'];
 }
 function timingOpt(o){
  const est=o.live_est!=null?o.live_est:o.current_bid;
  if(est==null)return['NO LIVE MARK','warn'];
  if(est<=o.stop_premium)return['BROKEN — at/below stop','neg'];
  if(est>=o.target_premium*0.9)return['TOO LATE — near target','neg'];
  if(est<=o.entry_ask*1.15)return['OPTIMAL — near entry premium','pos'];
  if(est<=o.entry_ask*1.5)return['EXTENDED — premium up '+Math.round(100*(est/o.entry_ask-1))+'%','warn'];
  return['TOO LATE — premium up '+Math.round(100*(est/o.entry_ask-1))+'%','neg'];
 }
 const stockCards=by('vol24').map(p=>{
  const [txt,cl]=timingStock(p);
  return`<div class="scard"><span class="tk">${p.ticker}</span><span class="tag">STOCK CALL</span><span class="dir ${p.direction==='bull'?'pos':'neg'}" style="float:right">${p.direction==='bull'?'LONG':'SHORT'}</span>
  <table><tr><td>Live price</td><td style="text-align:right"><b>${fmt$(p.current_price)}</b>${liveStamp?' <span class="chip bullish" style="font-size:8px">LIVE</span>':''}</td></tr>
  <tr><td>Entry (flag)</td><td style="text-align:right">${fmt$(p.flag_price)}</td></tr>
  <tr><td>Stop</td><td style="text-align:right" class="neg">${fmt$(p.stop_price)}</td></tr></table>
  <div style="margin-top:6px"><span class="chip ${cl==='pos'?'bullish':cl==='neg'?'bearish':'neutral'}" style="margin-left:0">${txt}</span></div>
  <div class="nm" style="margin-top:4px">stock position · graded ${p.eval_date.slice(5)}</div></div>`;
 });
 const optCards=D.options.map(o=>{
  const [txt,cl]=timingOpt(o);
  const u=D.positions.find(p=>p.ticker===o.underlying);
  const est=o.live_est!=null?o.live_est:o.current_bid;
  return`<div class="scard" style="border-color:color-mix(in srgb,var(--warn) 40%,var(--border))"><span class="tk">${o.underlying}</span><span class="tag" style="color:var(--warn);border-color:var(--warn)">OPTION</span><span class="dir ${o.type==='call'?'pos':'neg'}" style="float:right">${o.type==='call'?'LONG':'SHORT'}</span>
  <div class="nm" style="margin-top:2px">${o.type.toUpperCase()} $${o.strike} exp ${o.expiry.slice(5)} · ${o.contracts_n} contract(s) = ${fmt$(o.cost)} of $250 budget</div>
  <table>${u?`<tr><td>Stock live</td><td style="text-align:right"><b>${fmt$(u.current_price)}</b>${liveStamp?' <span class="chip bullish" style="font-size:8px">LIVE</span>':''}</td></tr>`:''}
  <tr><td>Premium in</td><td style="text-align:right">${fmt$(o.entry_ask)}</td></tr>
  <tr><td>Premium now</td><td style="text-align:right"><b>${est!=null?fmt$(est):'—'}</b>${o.live_est!=null?' <span class="nm">est</span>':''}</td></tr>
  <tr><td>Target sale</td><td style="text-align:right" class="pos">${fmt$(o.target_premium)}</td></tr>
  <tr><td>Stop loss</td><td style="text-align:right" class="neg">${fmt$(o.stop_premium)}</td></tr></table>
  <div style="margin-top:6px"><span class="chip ${cl==='pos'?'bullish':cl==='neg'?'bearish':'neutral'}" style="margin-left:0">${txt}</span></div>
  <div class="nm" style="margin-top:4px">forced exit ${o.exit_by.slice(5)} · R:R ${((o.target_premium-o.entry_ask)/(o.entry_ask-o.stop_premium)).toFixed(1)}</div></div>`;
 });
 const cardHtml=stockCards.concat(optCards).join('');
 const cards={length:stockCards.length+optCards.length};
 // performance summary + drawdown from real (small) history — no fabricated numbers
 const realized=[...D.closed,...D.closed_options];
 const wins=realized.filter(t=>t.pl>0), losses=realized.filter(t=>t.pl<=0);
 const gw=wins.reduce((s2,t)=>s2+t.pl,0), gl=Math.abs(losses.reduce((s2,t)=>s2+t.pl,0));
 const pf=gl>0?(gw/gl).toFixed(2):(gw>0?'∞':'—');
 const H=D.history; let maxdd=0,peak=-1e9;
 H.forEach(h=>{peak=Math.max(peak,h.value);maxdd=Math.min(maxdd,h.value/peak-1);});
 const small=realized.length<20?' <span class="nm">(n='+realized.length+' — too small to trust)</span>':'';
 const perf=[
  ['Resolved trades',realized.length,''],['Win rate',realized.length?Math.round(100*wins.length/realized.length)+'%':'—',''],
  ['Profit factor',pf,''],['Expectancy',realized.length?fmt$(realized.reduce((s2,t)=>s2+t.pl,0)/realized.length):'—','per trade'],
  ['Max drawdown',`<span class="${maxdd<0?'neg':''}">${(100*maxdd).toFixed(1)}%</span>`,H.length+' days of history'],
 ].map(([l,v,s2])=>`<div class="tile"><div class="lbl">${l}</div><div class="val">${v}</div><div class="sm">${s2}</div></div>`).join('');
 let ddSvg='';
 if(H.length>=2){
  const W=Math.min(document.body.clientWidth-60,1100),Hh=110,padL=50,padB=16,padT=6;
  let pk=-1e9;const dd=H.map(h=>{pk=Math.max(pk,h.value);return h.value/pk-1;});
  const mn=Math.min(...dd,-0.001);
  const x=i=>padL+i/(H.length-1)*(W-padL-8),y=v=>padT+(0-v)/(0-mn)*(Hh-padT-padB);
  ddSvg=`<svg width="${W}" height="${Hh}"><line x1="${padL}" x2="${W-4}" y1="${y(0)}" y2="${y(0)}" stroke="${css('--axis')}"/><text x="${padL-5}" y="${y(0)+3}" text-anchor="end">0%</text><text x="${padL-5}" y="${y(mn)+3}" text-anchor="end">${(100*mn).toFixed(1)}%</text><polyline points="${dd.map((v,i)=>x(i)+','+y(v)).join(' ')}" fill="none" stroke="${css('--down')}" stroke-width="2"/></svg>`;
 }
 return`<div class="kpis">${kpis}</div>
 <div class="panel"><h2>Live signals — stock calls vs option contracts (updates every 60s with live quotes)</h2>${cards.length?`<div class="sigcards">${cardHtml}</div>`:'<div class="empty">No open direction calls.</div>'}</div>
 <div class="panel"><h2>Active world signals</h2><div class="signals">${sigs.map(g=>`<div class="sig"><b>${g.title}<span class="chip ${g.sentiment}">${g.sentiment.toUpperCase()}</span>${g.sector?`<span class="tag">${g.sector}</span>`:''}</b><span>${g.body}</span></div>`).join('')}</div></div>
 <div class="panel"><h2>System performance — real numbers, no marketing${small}</h2><div class="kpis">${perf}</div>${ddSvg?`<h2 style="margin-top:10px">Drawdown</h2>${ddSvg}`:''}</div>
 <div class="panel"><h2>Portfolio vs same-day SPY stakes</h2>
  <div class="legend"><span><i style="background:${css('--s1')}"></i>Scanner</span><span><i style="background:${css('--muted')}"></i>SPY</span></div>${curveSvg()}</div>
 <div class="panel"><h2>Alpha since flag, all books</h2>${alphaBars()}</div>
 <div class="panel"><h2>Portfolio allocation — $1,000 across four books, weighted by conviction (options pool separate)</h2><div style="overflow-x:auto"><table><thead><tr><th style="text-align:left">Book</th><th>Pos</th><th>Allocated</th><th>Invested</th><th>Cash</th><th>Value</th><th>P/L</th><th>Weight</th></tr></thead><tbody>${bookRows}</tbody></table></div>
 <h2 style="margin-top:12px">Largest holdings</h2><div style="overflow-x:auto"><table><thead><tr><th style="text-align:left">Position</th><th>Stake</th><th>Value</th><th>P/L</th><th>% of portfolio</th></tr></thead><tbody>${holdings}</tbody></table></div></div>
 <div class="panel"><h2>Watchlist</h2><ul class="watch">${D.watchlist.map(w=>`<li><b>${w.ticker}</b> — ${w.note}</li>`).join('')}</ul></div>`;
}
function optionsView(){
 const os=D.opt_stats;
 const realized=D.closed_options.reduce((t,o)=>t+(o.pl||0),0);
 const bandCol=b=>b==='A'?css('--up'):b==='B'?'#4ea8de':b==='C'?css('--warn'):css('--down');
 const kpi=[
  ['Open contracts',D.options.length+' pos','$'+D.options.reduce((t,o)=>t+o.cost,0).toFixed(0)+' at risk · cap 8/day'],
  ['Open P/L'+(liveStamp?' (live est)':''),`<span class="${cls(D.options.reduce((t,o)=>t+(o.pl||0),0))}">${fmt$(D.options.reduce((t,o)=>t+(o.pl||0),0))}</span>`,'mark: '+(liveStamp?'model est':'CBOE bid')],
  ['Realized (closed)',`<span class="${cls(realized)}">${fmt$(realized)}</span>`,os.resolved+' resolved'],
  ['Win rate',os.resolved?`${os.wins}/${os.resolved} (${Math.round(100*os.wins/os.resolved)}%)`:'0 resolved','kill: P/L<0 after 20'],
  ['Avg confidence',(()=>{const s=D.options.filter(o=>o.conf!=null);return s.length?Math.round(s.reduce((t,o)=>t+o.conf,0)/s.length):'—'})(),'of open book'],
 ].map(([l,v,s2])=>`<div class="tile"><div class="lbl">${l}</div><div class="val">${v}</div><div class="sm">${s2}</div></div>`).join('');

 const card=o=>{
  const live=o.live_est!=null, mark=live?o.live_est:o.current_bid;
  const b=o.conf_band||'—', col=bandCol(b);
  const p=o.conf_parts||{}, dt=o.conf_detail||{};
  const toTgt=mark!=null&&o.target_premium?((o.target_premium-mark)/mark*100):null;
  const toStop=mark!=null&&o.stop_premium?((mark-o.stop_premium)/mark*100):null;
  const parts=[['Execution',p.execution,25,`spread ${dt.spread_pct}% · OI ${dt.oi}`],
   ['Breakeven',p.breakeven,25,`needs ${dt.required_move_pct}% vs ${dt.expected_move_pct}% implied — ratio ${dt.be_ratio}`],
   ['Signal',p.signal,25,dt.signal_tier||'—'],
   ['Clock',p.clock,15,`${dt.days_left_at_exit} days of life left at planned exit`],
   ['IV cost',p.iv_cost,10,dt.iv_vs_realized!=null?`IV is ${dt.iv_vs_realized}× 20-day realized`:'realized vol unavailable — neutral 5']]
   .map(([l,v,mx,d])=>`<tr><td style="text-align:left">${l}</td><td><b>${v==null?'—':v}</b>/${mx}</td><td style="width:32%"><div style="background:var(--grid);height:6px;border-radius:4px"><div style="background:${v/mx>=0.6?css('--up'):(v/mx<=0.35?css('--down'):css('--warn'))};height:6px;width:${Math.round(100*(v||0)/mx)}%;border-radius:4px"></div></div></td><td style="color:var(--muted);text-align:right;font-size:11px">${d}</td></tr>`).join('');
  return`<div class="ocard">
   <div class="ohead">
    <div class="oconf" style="border-color:${col}">
      <div class="oconfn" style="color:${col}">${o.conf!=null?o.conf:'—'}</div>
      <div class="oconfb" style="background:${col}">BAND ${b}</div>
      <div class="oconfl">confidence</div>
    </div>
    <div style="flex:1;min-width:0">
     <div class="otitle"><span class="tk">${o.underlying}</span> <span class="obig">$${o.strike} ${o.type.toUpperCase()}</span> <span class="tag">exp ${o.expiry.slice(5)}</span></div>
     <div class="okeys">
      <div class="okey"><span>Entry</span><b>${fmt$(o.entry_ask)}</b></div>
      <div class="okey"><span>Target</span><b class="pos">${fmt$(o.target_premium)}</b></div>
      <div class="okey"><span>Stop</span><b class="neg">${fmt$(o.stop_premium)}</b></div>
      <div class="okey"><span>Mark${live?' est':''}</span><b>${mark!=null?fmt$(mark):'—'}</b></div>
      <div class="okey"><span>Qty</span><b>${o.contracts_n} × ${fmt$(o.cost)}</b></div>
      <div class="okey"><span>P/L</span><b class="${cls(o.pl||0)}">${fmt$(o.pl||0)} <span style="font-size:11px">${fmtP(o.pl_pct||0)}</span></b></div>
     </div>
     ${o.status!=='closed'?timingStrip(o):''}
    <div class="obar">${toTgt!=null?`<span>${toTgt.toFixed(0)}% to target</span><span>·</span><span>${toStop.toFixed(0)}% to stop</span><span>·</span>`:''}<span>${o.status==='closed'?(o.exit_reason||'closed'):'exit by '+o.exit_by.slice(5)}</span></div>
    </div>
   </div>
   <details class="odet"><summary>Details, score breakdown &amp; thesis</summary>
    <table style="margin-top:8px"><tbody>${parts}</tbody></table>
    <div class="odl">
     <div><span>Contract</span>${o.contract}</div>
     <div><span>Underlying at entry</span>${o.entry_spot!=null?fmt$(o.entry_spot):'—'}</div>
     <div><span>Open interest</span>${o.oi}</div>
     <div><span>IV at entry</span>${(o.iv*100).toFixed(0)}%</div>
     <div><span>Entry bid/ask</span>${fmt$(o.entry_bid)} / ${fmt$(o.entry_ask)}</div>
     <div><span>Entry date</span>${o.entry_date}</div>
    </div>
    <div class="sig" style="margin-top:8px"><span><b>Source:</b> ${o.source_call||'—'}</span></div>
    ${o.note?`<div class="sig" style="margin-top:6px;border-color:var(--muted)"><span>${o.note}</span></div>`:''}
   </details></div>`;
 };
 const sorted=D.options.slice().sort((a,b)=>(b.conf||0)-(a.conf||0));
 const openT=sorted.length?sorted.map(card).join(''):'<div class="empty">No open contracts.</div>';

 const bg=D.conf_grade||{}; const bandOrder=['A','B','C','D'];
 const gradeRows=bandOrder.filter(b=>bg[b]).map(b=>{const d=bg[b];
   return`<tr><td style="text-align:left"><span class="oconfb" style="background:${bandCol(b)};display:inline-block">BAND ${b}</span></td><td>${d.n}</td><td class="${cls(d.avg_pl_pct)}"><b>${fmtP(d.avg_pl_pct)}</b></td><td>${d.win_rate}%</td><td class="${cls(d.sum_pl)}">${fmt$(d.sum_pl)}</td></tr>`}).join('');
 const kv=D.conf_kill||{};
 const gradePanel=`<div class="panel"><h2>Does the confidence score actually work?</h2>
  ${gradeRows?`<div style="overflow-x:auto"><table><thead><tr><th style="text-align:left">Band</th><th>Resolved</th><th>Avg P/L %</th><th>Win rate</th><th>Total $</th></tr></thead><tbody>${gradeRows}</tbody></table></div>`:'<div class="empty">No scored contracts have resolved yet.</div>'}
  <div class="sig" style="margin-top:10px;border-color:${kv.verdict==='dead'?css('--down'):kv.verdict==='alive'?css('--up'):css('--warn')}"><span><b>${(kv.verdict||'pending').toUpperCase()}</b> — ${kv.msg||''}</span></div></div>`;

 const closedT=D.closed_options.length?`<h2 style="margin-top:14px">Exited — realized results (permanent record)</h2><div style="overflow-x:auto"><table><thead><tr><th style="text-align:left">Contract</th><th>Conf</th><th>Qty</th><th>Entry</th><th>Exit bid</th><th>Realized $</th><th>Realized %</th><th>Exit date</th><th>Reason</th></tr></thead><tbody>${D.closed_options.map(o=>`<tr><td style="text-align:left"><span class="tk">${o.underlying}</span> <span class="tag">${o.type.toUpperCase()} $${o.strike}</span><br><span class="nm">${o.contract}</span></td><td>${o.conf!=null?`<b style="color:${bandCol(o.conf_band)}">${o.conf} ${o.conf_band}</b>`:'<span class="nm">unscored</span>'}</td><td>${o.contracts_n}</td><td>${fmt$(o.entry_ask)}</td><td>${fmt$(o.exit_bid!=null?o.exit_bid:o.current_bid)}</td><td class="${cls(o.pl)}" style="font-weight:650">${fmt$(o.pl)}</td><td class="${cls(o.pl_pct)}">${fmtP(o.pl_pct)}</td><td>${o.exit_date||''}</td><td style="color:var(--muted)">${o.exit_reason||''}</td></tr>`).join('')}</tbody></table></div>`:'';

 return`<div class="kpis">${kpi}</div>
 <div class="panel"><h2>Rules of this book</h2><div class="sig flow" style="border-color:var(--warn)"><span>Up to <b>8 contracts/day</b> across ETFs, blue chips, movers and earnings names. Gates are unchanged and non-negotiable: <b>2–10% OTM</b> (0.2–1.5% for 1DTE) · <b>OI ≥ 500</b> · <b>spread ≤ 20% of ask</b> · <b>cost ≤ $250</b>. Target = <b>6× entry ask</b>, stop = <b>0.5× ask</b> — a 10:1 ratio you set; forced exit after 2 trading days or at expiry, first trigger wins. Confidence <b>ranks</b> candidates, it does not gate them: every band gets traded so the score can be proven or killed. Prior study says the expected result is losing the premium; this book measures it and closes permanently if P/L is negative after 20 resolved positions.</span></div></div>
 ${gradePanel}
 <div class="panel"><h2>Open contracts — ranked by confidence</h2>
  <div class="tbar">${timingBtn('tOpt')}<span class="nm">Re-prices every contract from live stock quotes and tells you, per contract, whether entering right now still gets roughly the flagged price. Premiums are Black-Scholes estimates off the live stock price and entry IV — not real quotes.</span></div>
  ${openT}${closedT}</div>`;
}
function gauge(score){
 const w=260,h=90,cx=w/2,cy=h-6,r=64;
 const ang=Math.PI*(1-score/100);
 const x=cx+r*Math.cos(ang), y=cy-r*Math.sin(ang);
 const col=score>=60?css('--up'):(score<=40?css('--down'):css('--warn'));
 let s2=`<svg width="${w}" height="${h}">`;
 s2+=`<path d="M ${cx-r} ${cy} A ${r} ${r} 0 0 1 ${cx-r*0.31} ${cy-r*0.95}" fill="none" stroke="${css('--down')}" stroke-width="9" opacity=".45"/>`;
 s2+=`<path d="M ${cx-r*0.31} ${cy-r*0.95} A ${r} ${r} 0 0 1 ${cx+r*0.31} ${cy-r*0.95}" fill="none" stroke="${css('--muted')}" stroke-width="9" opacity=".45"/>`;
 s2+=`<path d="M ${cx+r*0.31} ${cy-r*0.95} A ${r} ${r} 0 0 1 ${cx+r} ${cy}" fill="none" stroke="${css('--up')}" stroke-width="9" opacity=".45"/>`;
 s2+=`<line x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" stroke="${col}" stroke-width="3"/>`;
 s2+=`<circle cx="${cx}" cy="${cy}" r="4" fill="${col}"/>`;
 s2+=`<text x="${cx}" y="${cy-22}" text-anchor="middle" style="font-size:22px;font-weight:700;fill:${col}">${score}</text>`;
 return s2+'</svg>';
}
function spyView(){
 const s=D.spy||{};
 if(!s.score)return '<div class="panel"><div class="empty">SPY model has not run yet — it populates on the next scan.</div></div>';
 const p=s.parts||{};
 const rows=[['Trend (SPY vs 20 & 50 DMA)',p.trend,30,`px ${fmt$(s.price)} · 20d ${fmt$(s.sma20)} · 50d ${fmt$(s.sma50)}`],
   ['Momentum (5-day return)',p.momentum,25,fmtP(s.ret5)],
   ['Volatility (VIX level + trend)',p.volatility,20,`VIX ${s.vix} (${fmtP(s.vix_chg5)} 5d)`],
   ['Breadth (sector ETFs > 20DMA)',p.breadth,15,`${s.breadth_pct}% of 8 sectors`],
   ['World news signals',p.news,10,'bullish minus bearish']]
  .map(([l,v,mx,d])=>`<tr><td style="text-align:left">${l}</td><td><b>${v}</b> / ${mx}</td><td style="width:34%"><div style="background:var(--grid);height:7px;border-radius:4px"><div style="background:${v/mx>=0.6?css('--up'):(v/mx<=0.35?css('--down'):css('--warn'))};height:7px;width:${Math.round(100*v/mx)}%;border-radius:4px"></div></div></td><td style="color:var(--muted);text-align:right">${d}</td></tr>`).join('');
 const gcalls=(D.spy_calls||[]).filter(c=>c.outcome);
 const hits=gcalls.filter(c=>c.outcome==='hit').length;
 const rate=gcalls.length?Math.round(100*hits/gcalls.length):null;
 // --- today's 1DTE contract ---------------------------------------
 const all1=[...D.options,...D.closed_options].filter(o=>(o.source_call||'').indexOf('1dte')===0);
 all1.sort((a,b)=>(a.entry_date<b.entry_date?1:-1));
 const c1=all1[0];
 const dteCard=(()=>{
  if(!c1)return`<div class="sig" style="border-color:var(--warn)"><span><b>No 1DTE contract selected yet.</b> The post-open scan picks exactly one strike per day. A day with none is a legitimate result — it means no strike cleared the $0.30–$0.60 / 0.2–1.5% OTM / OI≥500 / spread≤20% gates, or the bias was NEUTRAL.</span></div>`;
  const open=c1.status!=='closed';
  const live=c1.live_est!=null, mark=open?(live?c1.live_est:c1.current_bid):(c1.exit_bid!=null?c1.exit_bid:c1.current_bid);
  const col=c1.conf_band==='A'?css('--up'):c1.conf_band==='B'?'#4ea8de':c1.conf_band==='C'?css('--warn'):css('--down');
  const dist=s.price&&c1.strike?((c1.strike-s.price)/s.price*100):null;
  const box=(l,v,c)=>`<div class="dbox"><span>${l}</span><b class="${c||''}">${v}</b></div>`;
  return`<div class="dteWrap">
   <div class="dteMain">
    <div class="dteLbl">${open?'TODAY’S 1DTE CONTRACT':'TODAY’S 1DTE CONTRACT — CLOSED'}</div>
    <div class="dteStrike">SPY $${c1.strike} <span style="font-size:24px">${c1.type.toUpperCase()}</span></div>
    <div class="dteSub">${c1.contract} · expires ${c1.expiry}${dist!=null?` · strike is ${dist>=0?'+':''}${dist.toFixed(2)}% from SPY ${fmt$(s.price)} right now`:''}</div>
   </div>
   <div class="dteGrid">
    ${box('Entry (ask)',fmt$(c1.entry_ask))}
    ${box('Target',fmt$(c1.target_premium),'pos')}
    ${box('Stop',fmt$(c1.stop_premium),'neg')}
    ${box(open?(live?'Mark (est)':'Mark'):'Exit bid',mark!=null?fmt$(mark):'—')}
    ${box('Contracts',c1.contracts_n+' × '+fmt$(c1.cost))}
    ${box(open?'Open P/L':'Realized',fmt$(c1.pl||0)+' ('+fmtP(c1.pl_pct||0)+')',cls(c1.pl||0))}
    ${c1.conf!=null?`<div class="dbox" style="border-color:${col}"><span>Confidence</span><b style="color:${col}">${c1.conf} · ${c1.conf_band}</b></div>`:''}
    ${box('Status',open?('exit by '+c1.exit_by):(c1.exit_reason||'closed'),open?'':'neg')}
   </div>
   <div style="margin-top:10px">${timingStrip(c1)}</div>
   ${c1.source_call?`<div class="sig" style="margin-top:9px"><span><b>Why this strike:</b> ${c1.source_call}</span></div>`:''}
  </div>`;
 })();
 const prior1=all1.slice(1,11).map(o=>`<tr><td style="text-align:left">${o.entry_date}</td><td><b>$${o.strike} ${o.type.toUpperCase()}</b></td><td>${fmt$(o.entry_ask)}</td><td class="pos">${fmt$(o.target_premium)}</td><td class="neg">${fmt$(o.stop_premium)}</td><td>${o.contracts_n}</td><td class="${cls(o.pl||0)}"><b>${fmt$(o.pl||0)}</b></td><td style="color:var(--muted)">${o.exit_reason||'open'}</td></tr>`).join('');
 const callRows=(D.spy_calls||[]).slice().reverse().slice(0,15).map(c=>
  `<tr><td style="text-align:left">${c.date}</td><td><span class="dir ${c.bias==='BULLISH'?'pos':(c.bias==='BEARISH'?'neg':'')}">${c.bias}</span></td><td>${c.score}</td><td>${c.spy_close?fmt$(c.spy_close):'—'}</td><td class="${c.next_ret!=null?cls(c.next_ret):''}">${c.next_ret!=null?fmtP(c.next_ret):'pending'}</td><td>${c.outcome?(c.outcome==='hit'?'<span class="pos">✓ hit</span>':'<span class="neg">✗ miss</span>'):'<span class="nm">—</span>'}</td></tr>`).join('');
 return `<div class="kpis">
  <div class="tile"><div class="lbl">SPY</div><div class="val">${fmt$(s.price)}</div><div class="sm ${cls(s.day_pct)}">${fmtP(s.day_pct)} today</div></div>
  <div class="tile"><div class="lbl">Composite bias</div><div class="val" style="color:${s.bias==='BULLISH'?css('--up'):(s.bias==='BEARISH'?css('--down'):css('--warn'))}">${s.bias}</div><div class="sm">score ${s.score}/100</div></div>
  <div class="tile"><div class="lbl">Direction hit rate</div><div class="val">${rate!=null?rate+'%':'—'}</div><div class="sm">${gcalls.length} graded · kill &lt;55% @ 30</div></div>
  <div class="tile"><div class="lbl">VIX</div><div class="val">${s.vix}</div><div class="sm ${cls(-s.vix_chg5)}">${fmtP(s.vix_chg5)} 5d</div></div>
  <div class="tile"><div class="lbl">Breadth</div><div class="val">${s.breadth_pct}%</div><div class="sm">sectors above 20-DMA</div></div>
 </div>
 <div class="panel" style="text-align:center"><h2>Daily sentiment</h2>${gauge(s.score)}
  <div class="nm" style="margin-top:4px">0—40 bearish · 40—60 neutral · 60—100 bullish · weights frozen 2026-08-13, never re-tuned</div></div>
 <div class="panel"><h2>Score components</h2><div style="overflow-x:auto"><table><tbody>${rows}</tbody></table></div></div>
 <div class="panel"><h2>Today’s 1DTE contract — the one strike</h2>
  <div class="tbar">${timingBtn('tSpy')}<span class="nm">Pulls a live SPY quote, re-prices the strike, and says whether now is still a reasonable entry or the move is already gone.</span></div>
  ${dteCard}</div>
 <div class="panel"><h2>Live entry check — is a fresh SPY 1DTE worth taking right now?</h2>
  <div class="tbar">${timingBtn('tFresh')}<span class="nm">Runs the same gates against the most recent chain scan and re-prices from a live SPY quote. Answers "would a new contract qualify now", separately from how today's tracked contract did.</span></div>
  ${freshPanel()}</div>
 ${prior1?`<div class="panel"><h2>Prior 1DTE contracts</h2><div style="overflow-x:auto"><table><thead><tr><th style="text-align:left">Date</th><th>Strike</th><th>Entry</th><th>Target</th><th>Stop</th><th>Qty</th><th>P/L</th><th>Outcome</th></tr></thead><tbody>${prior1}</tbody></table></div></div>`:''}
 <div class="panel"><h2>1DTE rules</h2><div class="sig flow" style="border-color:var(--warn)"><span>SPY only · exactly <b>one strike</b> per day · premium <b>$0.30–$0.60</b> · <b>0.2–1.5% OTM</b> (the 2–10% gate is meaningless at one day) · OI ≥ 500 · spread ≤ 20% · ≤ $250 total · target 6× / stop 0.5× · NEUTRAL days trade nothing. The direction call is published and graded <b>every day regardless</b>, so direction skill is measured separately from option economics — per the 811-day study, that separation is the whole point. Prior: 0–2DTE SPY OTM had median −100% per trade.</span></div></div>
 <div class="panel"><h2>Direction call record — published before the session, graded after</h2>${(D.spy_calls||[]).length?`<div style="overflow-x:auto"><table><thead><tr><th style="text-align:left">Call date</th><th>Bias</th><th>Score</th><th>SPY at call</th><th>Next session</th><th>Result</th></tr></thead><tbody>${callRows}</tbody></table></div>`:'<div class="empty">First call publishes on the next scan.</div>'}</div>`;
}
function whaleView(){
 const w=D.whale||{};
 const ev=D.whale_events||[];
 const notes=Object.entries(w.notes||{}).map(([k,v])=>`<div class="sig"><b>${k}</b><span>${v}</span></div>`).join('');
 const evRows=ev.slice().reverse().slice(0,25).map(e=>
  `<tr><td style="text-align:left">${e.date||''}</td><td><span class="tag">${e.form||''}</span></td><td style="text-align:left"><b>${e.filer||''}</b></td><td style="text-align:left">${e.subject||''}</td><td style="color:var(--muted);text-align:left">${e.note||''}</td></tr>`).join('');
 return `<div class="kpis">
  <div class="tile"><div class="lbl">Next 13F deadline</div><div class="val">${w.next_13f_deadline||'—'}</div><div class="sm">${w.days_to_13f!=null?w.days_to_13f+' day(s) away':''}</div></div>
  <div class="tile"><div class="lbl">13F window</div><div class="val" style="color:${w.in_13f_window?css('--warn'):css('--muted')}">${w.in_13f_window?'OPEN':'closed'}</div><div class="sm">institutions filing now</div></div>
  <div class="tile"><div class="lbl">Events logged</div><div class="val">${ev.length}</div><div class="sm">filings captured</div></div>
 </div>
 ${w.in_13f_window?`<div class="panel"><h2>Active window</h2><div class="sig" style="border-color:var(--warn)"><span><b>13F filings are landing now</b> — institutions with over $100M must disclose Q2 holdings by ${w.next_13f_deadline}. Read these as sentiment only: the positions are up to 4.5 months old and the fund may have already exited. Nothing here is a trade signal on its own.</span></div></div>`:''}
 <div class="panel"><h2>When large-holder information actually surfaces</h2><div class="signals">${notes}</div></div>
 <div class="panel"><h2>Captured filings</h2>${ev.length?`<div style="overflow-x:auto"><table><thead><tr><th style="text-align:left">Date</th><th>Form</th><th style="text-align:left">Filer</th><th style="text-align:left">Subject</th><th style="text-align:left">Note</th></tr></thead><tbody>${evRows}</tbody></table></div>`:'<div class="empty">No filings captured yet — the scans populate this from SEC EDGAR feeds (Form 4 insider trades, 13D activist stakes, 13F institutional holdings).</div>'}</div>`;
}
function journalView(){
 const trades=[...D.closed.map(t=>({...t,kind:'stock',label:t.ticker,entry:t.flag_price,exit:t.exit_price,date:t.exit_date,book:t.book||'core'})),
               ...D.closed_options.map(o=>({...o,kind:'option',label:o.underlying+' '+o.type.toUpperCase()+' $'+o.strike,entry:o.entry_ask,exit:o.exit_bid!=null?o.exit_bid:o.current_bid,date:o.exit_date,book:'options'}))]
  .sort((a,b)=>String(b.date).localeCompare(String(a.date)));
 const wins=trades.filter(t=>t.pl>0),losses=trades.filter(t=>t.pl<=0);
 const gw=wins.reduce((s2,t)=>s2+t.pl,0),gl=Math.abs(losses.reduce((s2,t)=>s2+t.pl,0));
 const kpi=[
  ['Total realized P/L',`<span class="${cls(trades.reduce((s2,t)=>s2+t.pl,0))}">${fmt$(trades.reduce((s2,t)=>s2+t.pl,0))}</span>`,trades.length+' closed trades'],
  ['Win rate',trades.length?Math.round(100*wins.length/trades.length)+'% ('+wins.length+'/'+trades.length+')':'—',''],
  ['Profit factor',gl>0?(gw/gl).toFixed(2):(gw>0?'∞':'—'),''],
  ['Avg win / avg loss',(wins.length?fmt$(gw/wins.length):'—')+' / '+(losses.length?fmt$(-gl/losses.length):'—'),''],
 ].map(([l,v,s2])=>`<div class="tile"><div class="lbl">${l}</div><div class="val">${v}</div><div class="sm">${s2}</div></div>`).join('');
 const byBook={};trades.forEach(t=>{byBook[t.book]=byBook[t.book]||{n:0,pl:0};byBook[t.book].n++;byBook[t.book].pl+=t.pl;});
 const worst=Object.entries(byBook).sort((a,b)=>a[1].pl-b[1].pl)[0];
 const breakdown=Object.entries(byBook).map(([b,v])=>`<tr><td style="text-align:left">${b}</td><td>${v.n}</td><td class="${cls(v.pl)}">${fmt$(v.pl)}</td></tr>`).join('');
 const rows=trades.map(t=>`<tr><td style="text-align:left"><span class="tk">${t.label}</span> <span class="tag">${t.kind}</span></td><td>${t.book}</td><td>${t.flag_date||t.entry_date||''}</td><td>${t.date||''}</td><td>${fmt$(t.entry)}</td><td>${fmt$(t.exit||0)}</td><td class="${cls(t.pl)}" style="font-weight:650">${fmt$(t.pl)}</td><td class="${cls(t.pl_pct)}">${fmtP(t.pl_pct)}</td><td style="color:var(--muted)">${t.exit_reason||t.direction_outcome||''}</td></tr>`).join('');
 return`<div class="kpis">${kpi}</div>
 ${worst&&worst[1].pl<0?`<div class="panel"><h2>Biggest weakness (so far)</h2><div class="sig" style="border-color:var(--down)"><span>The <b>${worst[0]}</b> book accounts for ${fmt$(worst[1].pl)} of realized losses across ${worst[1].n} trade(s). Its kill rule decides its fate — not vibes.</span></div></div>`:''}
 <div class="panel"><h2>Breakdown by book</h2><div style="overflow-x:auto"><table><thead><tr><th style="text-align:left">Book</th><th>Trades</th><th>Realized P/L</th></tr></thead><tbody>${breakdown||''}</tbody></table></div></div>
 <div class="panel"><h2>Every realized trade — permanent record</h2>${trades.length?`<div style="overflow-x:auto"><table><thead><tr><th style="text-align:left">Trade</th><th>Book</th><th>Entered</th><th>Exited</th><th>Entry</th><th>Exit</th><th>P/L $</th><th>P/L %</th><th>Reason</th></tr></thead><tbody>${rows}</tbody></table></div>`:'<div class="empty">No closed trades yet.</div>'}</div>`;
}
function closedView(){
 if(!D.closed.length)return'<div class="panel"><div class="empty">None yet. Positions close at stops, at 5-day direction evaluations, or on thesis invalidation.</div></div>';
 return'<div class="panel"><div style="overflow-x:auto"><table><thead><tr><th style="text-align:left">Position</th><th>Book</th><th>Flagged</th><th>Flag</th><th>Exited</th><th>Exit</th><th>P/L %</th><th>Reason</th></tr></thead><tbody>'+
  D.closed.map(p=>`<tr><td style="text-align:left"><span class="tk">${p.ticker}</span></td><td>${p.book||'core'}</td><td>${p.flag_date}</td><td>${fmt$(p.flag_price)}</td><td>${p.exit_date||''}</td><td>${fmt$(p.exit_price||p.current_price)}</td><td class="${cls(p.pl_pct)}">${fmtP(p.pl_pct)}</td><td style="color:var(--muted)">${p.exit_reason||''}</td></tr>`).join('')+'</tbody></table></div></div>';
}
// ---- live quotes (Finnhub, browser-side; enabled when a key is baked in) ----
const FKEY='__FINNHUB_KEY__';
let liveStamp=null;
async function liveTick(){
 if(!FKEY||FKEY.startsWith('__'))return;
 try{
  const tickers=[...new Set(D.positions.map(p=>p.ticker))].concat(['SPY']);
  const qs=await Promise.all(tickers.map(t=>
   fetch('https://finnhub.io/api/v1/quote?symbol='+t+'&token='+FKEY).then(r=>r.json()).catch(()=>null)));
  const map={};tickers.forEach((t,i)=>{if(qs[i]&&qs[i].c)map[t]=qs[i];});
  const spy=map['SPY'];
  if(spy&&spy.c&&D.spy){D.spy.price=spy.c;if(spy.pc)D.spy.day_pct=+(100*(spy.c/spy.pc-1)).toFixed(2);}
  let touched=0;
  D.positions.forEach(p=>{
   const q=map[p.ticker];if(!q)return;touched++;
   p.current_price=q.c;
   if(q.pc)p.day_pct=+(100*(q.c/q.pc-1)).toFixed(2);
   p.value=+(p.shares*q.c).toFixed(2);
   p.pl=+(p.value-(p.stake||0)).toFixed(2);
   p.pl_pct=+((q.c/p.flag_price-1)*100).toFixed(2);
   if(spy&&p.spy_flag_price){
    const sr=100*(spy.c/p.spy_flag_price-1);
    p.spy_ret_pct=+sr.toFixed(2);
    p.alpha_pct=+(p.pl_pct-sr).toFixed(2);
   }
   if(p.spark&&p.spark.length)p.spark[p.spark.length-1]=q.c;
  });
  // model-estimate open option premiums from live underlying (entry IV, r=4%)
  const ncdf=x=>{const t=1/(1+0.2316419*Math.abs(x));const d=0.3989423*Math.exp(-x*x/2);let pr=d*t*(0.3193815+t*(-0.3565638+t*(1.781478+t*(-1.821256+t*1.330274))));return x>0?1-pr:pr;};
  D.options.forEach(o=>{
   const q=map[o.underlying];if(!q||!o.iv)return;
   const S=q.c,K=o.strike;
   const T=Math.max((new Date(o.expiry+'T20:00:00Z')-new Date())/(365.25*864e5),1e-4);
   const v=Math.max(o.iv,0.05),r=0.04;
   const d1=(Math.log(S/K)+(r+v*v/2)*T)/(v*Math.sqrt(T)),d2=d1-v*Math.sqrt(T);
   let prem=o.type==='call'?S*ncdf(d1)-K*Math.exp(-r*T)*ncdf(d2):K*Math.exp(-r*T)*ncdf(-d2)-S*ncdf(-d1);
   prem=Math.max(prem,0);
   o.live_est=+prem.toFixed(2);
   o.value=+(prem*100*o.contracts_n).toFixed(2);
   o.pl=+(o.value-o.cost).toFixed(2);
   o.pl_pct=+(100*o.pl/o.cost).toFixed(1);
   touched++;
  });
  if(touched){
   D.total_val=D.positions.reduce((s,p)=>s+p.value,0);
   liveStamp=new Date().toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',hour12:true,timeZone:'America/New_York'})+' ET';
   setStamp();render();
  }
 }catch(e){/* keep scan-time values on any failure */}
}
async function manualRefresh(){
 const b=document.getElementById('rescan');
 if(!FKEY||FKEY.startsWith('__')){b.textContent='no live key';setTimeout(()=>b.textContent='⟳ Refresh prices',2000);return;}
 b.textContent='⟳ refreshing…';b.disabled=true;
 await liveTick();
 b.textContent='✓ refreshed';b.disabled=false;
 setTimeout(()=>b.textContent='⟳ Refresh prices',2500);
}

// ---- entry-timing verdict -------------------------------------------------
// Answers one question: if you were acting on this flag RIGHT NOW, would you
// be getting roughly the flagged price, a worse one, or is it already over?
// The premium used is a Black-Scholes estimate from the LIVE stock price and
// the ENTRY implied vol -- it is not a real quote. IV moves during the day, so
// treat this as a direction-of-travel read, not a fill price.
function mktState(){
 const n=new Date();
 const et=new Date(n.toLocaleString('en-US',{timeZone:'America/New_York'}));
 const dow=et.getDay(), mins=et.getHours()*60+et.getMinutes();
 if(dow===0||dow===6)return{open:false,left:0,label:'weekend'};
 if(mins<570)return{open:false,left:0,label:'pre-market'};
 if(mins>=960)return{open:false,left:0,label:'after hours'};
 return{open:true,left:960-mins,label:'open'};
}
function timing(o){
 const m=mktState();
 const mark=o.live_est!=null?o.live_est:o.current_bid;
 const est=o.live_est!=null;
 if(o.status==='closed')return{v:'CLOSED',c:'var(--muted)',t:'This contract already exited — '+(o.exit_reason||'closed')+'. Not an entry.'};
 if(mark==null)return{v:'NO DATA',c:'var(--muted)',t:'No live mark available. Hit the timing button to pull fresh prices.'};
 if(o.target_premium&&mark>=o.target_premium)return{v:'TOO LATE',c:'var(--warn)',t:'Already at or past the target ('+fmt$(o.target_premium)+'). The move happened. This is an exit, not an entry.'};
 if(o.stop_premium&&mark<=o.stop_premium)return{v:'DEAD',c:'var(--down)',t:'At or below the stop ('+fmt$(o.stop_premium)+'). The flagged trade is over — entering now is a different trade with no thesis behind it.'};
 const dte=Math.ceil((new Date(o.expiry+'T20:00:00Z')-new Date())/864e5);
 if(!m.open)return{v:'MARKET '+m.label.toUpperCase(),c:'var(--muted)',t:'You cannot get a real fill now. Quotes outside 9:30–4:00 ET are indicative only, and spreads widen sharply.'};
 if(dte<=1&&m.left<90)return{v:'TOO LATE',c:'var(--down)',t:'Under '+m.left+' minutes left and the contract expires within a day. Theta dominates everything else in the final hour — this is the worst point on the clock to open.'};
 const p=mark/o.entry_ask, ch=(p-1)*100;
 if(p>1.30)return{v:'TOO LATE',c:'var(--down)',t:'Premium is '+ch.toFixed(0)+'% above the flagged entry of '+fmt$(o.entry_ask)+'. You would be paying '+fmt$(mark)+' for the same contract — the move you were trying to catch has largely happened, and the distance left to target shrank with it.'};
 if(p>1.10)return{v:'LATE',c:'var(--warn)',t:'Premium is '+ch.toFixed(0)+'% above the flagged '+fmt$(o.entry_ask)+'. Still live, but you are buying after part of the move. Your effective risk/reward is worse than the one on the card.'};
 if(p>=0.85)return{v:'OPTIMAL',c:'var(--up)',t:'Premium is within '+Math.abs(ch).toFixed(0)+'% of the flagged entry ('+fmt$(o.entry_ask)+' → '+fmt$(mark)+'). Effectively the same trade the scanner flagged.'};
 return{v:'CHEAPER — CHECK THESIS',c:'#4ea8de',t:'Premium is '+Math.abs(ch).toFixed(0)+'% BELOW the flagged entry. Cheaper is not automatically better: the underlying has moved against the direction call, so the discount is the market disagreeing with the thesis, not a gift.'};
}
function timingStrip(o){
 const t=timing(o);
 const src=o.live_est!=null?`live estimate · ${liveStamp||''}`:`scan-time CBOE bid · ${D.stamp||''} — press the timing button for a live re-price`;
 return`<div class="tstrip" style="border-color:${t.c}"><span class="tbadge" style="background:${t.c}">${t.v}</span><span class="ttext">${t.t}<br><span class="nm" style="font-size:10.5px">priced from: ${src}</span></span></div>`;
}
function timingBtn(id){
 return`<button class="toggle tbtn" id="${id}" onclick="checkTiming('${id}')" style="border-color:var(--s1);color:var(--s1)">⟳ Check entry timing now</button>`;
}
async function checkTiming(id){
 const b=document.getElementById(id);
 if(!FKEY||FKEY.startsWith('__')){b.textContent='no live key';return;}
 b.textContent='⟳ checking…';b.disabled=true;
 await liveTick();
 render();
 const nb=document.getElementById(id);
 if(nb){nb.textContent='✓ updated '+new Date().toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',hour12:true,timeZone:'America/New_York'})+' ET';setTimeout(()=>{const z=document.getElementById(id);if(z)z.textContent='⟳ Check entry timing now'},4000);}
}


// ---- fresh SPY 1DTE entry check ------------------------------------------
// Different question from timing(): there is no flagged entry to compare
// against, so this asks "would a NEW contract clear the gates and the clock
// right now?" Gates are evaluated on the scan-time chain (real bid/ask/OI);
// the premium is then re-estimated from the live SPY quote, and the estimate
// is labelled as such. It is a check, not a fill.
function freshEntry(){
 const C=D.spy_cand;
 if(!C)return{v:'NO SCAN',c:'var(--muted)',t:'No live candidate has been scanned yet.',cand:null,est:null};
 const m=mktState(), s=D.spy||{};
 const b=C.best;
 if(C.bias==='NEUTRAL')return{v:'NO TRADE',c:'var(--muted)',t:'The composite is NEUTRAL. The rules trade nothing on neutral days — that is the rule doing its job, not a missed opportunity.',cand:b,est:null};
 if(!b)return{v:'NO ENTRY',c:'var(--down)',t:'No strike cleared all gates on the last chain scan. Near-misses and the gate each one failed are listed below.',cand:null,est:null};
 // re-estimate the premium from the live SPY price
 let est=null;
 if(liveStamp&&s.price&&b.iv){
  const ncdf=x=>{const t=1/(1+0.2316419*Math.abs(x));const d=0.3989423*Math.exp(-x*x/2);let pr=d*t*(0.3193815+t*(-0.3565638+t*(1.781478+t*(-1.821256+t*1.330274))));return x>0?1-pr:pr;};
  const S1=s.price,K=b.strike,T=Math.max((new Date(C.expiry+'T20:00:00Z')-new Date())/(365.25*864e5),1e-4);
  const v=Math.max(b.iv,0.05),r=0.04;
  const d1=(Math.log(S1/K)+(r+v*v/2)*T)/(v*Math.sqrt(T)),d2=d1-v*Math.sqrt(T);
  est=Math.max(S1*ncdf(d1)-K*Math.exp(-r*T)*ncdf(d2),0);
  est=+est.toFixed(2);
 }
 if(!m.open)return{v:'MARKET '+m.label.toUpperCase(),c:'var(--muted)',t:'No real fill is available outside 9:30–4:00 ET. Short-dated spreads widen hard after the bell — the $'+b.ask+' ask on the card is a regular-session number.',cand:b,est:est};
 if(m.left<90)return{v:'TOO LATE',c:'var(--down)',t:'Only '+(m.left>=60?Math.floor(m.left/60)+'h '+(m.left%60)+'m':m.left+' minutes')+' to the close on a contract expiring tomorrow. Theta is the dominant term in the last hour and it runs against a long buyer every minute — this is the worst point on the clock to open a 1DTE.',cand:b,est:est};
 const moved=s.day_pct;
 if(C.bias==='BULLISH'&&moved!=null&&moved>0.6)return{v:'LATE',c:'var(--warn)',t:'SPY is already +'+moved.toFixed(2)+'% today. A 1DTE bullish contract is a bet on the NEXT day\'s move, but most of the daily range is spent — you would be paying for a move that largely happened.',cand:b,est:est};
 if(C.bias==='BEARISH'&&moved!=null&&moved<-0.6)return{v:'LATE',c:'var(--warn)',t:'SPY is already '+moved.toFixed(2)+'% today. Most of the daily range is spent and you would be paying for a move that largely happened.',cand:b,est:est};
 if(est==null)return{v:'RE-SCAN NEEDED',c:'var(--warn)',t:'No live SPY quote has landed this session, so the strike cannot be re-priced against right now. The gates below passed at the '+C.scanned_at+' chain scan with SPY at '+fmt$(C.spot_at_scan)+' — press the button above to pull a live quote before trusting any of it.',cand:b,est:null};
 if(est!=null&&est>b.ask*1.25)return{v:'LATE',c:'var(--warn)',t:'The strike has richened since the chain scan — estimated '+fmt$(est)+' vs the scanned ask of '+fmt$(b.ask)+'. Re-scan before acting; the qty and cost on the card assume the scanned price.',cand:b,est:est};
 if(est!=null&&est<b.ask*0.6)return{v:'CHECK DIRECTION',c:'#4ea8de',t:'The strike is much cheaper than at scan ('+fmt$(est)+' est vs '+fmt$(b.ask)+' scanned) because SPY moved away from it. Cheaper, but the '+C.bias.toLowerCase()+' thesis is losing in real time.',cand:b,est:est};
 return{v:'ENTRY AVAILABLE',c:'var(--up)',t:'This strike clears every gate — '+b.otm+'% OTM, '+b.spread_pct+'% spread, OI '+b.oi.toLocaleString()+', '+b.contracts_n+' contracts for '+fmt$(b.cost)+' — with '+Math.floor(m.left/60)+'h '+(m.left%60)+'m left in the session. Clearing the gates is not a prediction: the prior study still says the median outcome for this trade is −100%.',cand:b,est:est};
}
function freshPanel(){
 const C=D.spy_cand; const f=freshEntry(); const b=f.cand;
 const box=(l,v,c)=>`<div class="dbox"><span>${l}</span><b class="${c||''}">${v}</b></div>`;
 const near=(C&&C.near_misses&&C.near_misses.length)?`<details class="odet" style="margin-top:9px"><summary>Strikes that did NOT qualify, and the gate each one failed</summary><table style="margin-top:7px"><thead><tr><th style="text-align:left">Strike</th><th>OTM</th><th>Ask</th><th>Spread</th><th>OI</th><th style="text-align:right">Failed gate</th></tr></thead><tbody>${C.near_misses.map(n=>`<tr><td style="text-align:left">$${n.strike}</td><td>${n.otm}%</td><td>${fmt$(n.ask)}</td><td>${n.spread_pct}%</td><td>${n.oi.toLocaleString()}</td><td style="text-align:right;color:var(--muted)">${n.fail}</td></tr>`).join('')}</tbody></table></details>`:'';
 const alt=(C&&C.passing&&C.passing.length>1)?`<div class="nm" style="margin-top:7px">Also cleared the gates: ${C.passing.filter(p=>!b||p.strike!==b.strike).map(p=>`$${p.strike} at ${fmt$(p.ask)} (conf ${p.conf} ${p.conf_band})`).join(' · ')} — the rules take the single highest-scoring survivor.</div>`:'';
 return`<div class="fresh" style="border-color:${f.c}">
  <div class="tstrip" style="border:0;padding:0;margin:0 0 10px;background:none"><span class="tbadge" style="background:${f.c}">${f.v}</span><span class="ttext">${f.t}</span></div>
  ${b?`<div class="dteStrike" style="font-size:30px">SPY $${b.strike} CALL <span style="font-size:14px;color:var(--muted);font-weight:500">exp ${C.expiry}</span></div>
  <div class="dteGrid" style="margin-top:9px">
   ${box('Ask at scan',fmt$(b.ask))}
   ${box('Est. now',f.est!=null?fmt$(f.est):'stale')}
   ${box('SPY scan → now',fmt$(C.spot_at_scan)+(liveStamp&&D.spy&&D.spy.price?' → '+fmt$(D.spy.price):' → —'),(liveStamp&&D.spy&&D.spy.price)?cls(D.spy.price-C.spot_at_scan):'')}
   ${box('Target',fmt$(b.target_premium),'pos')}
   ${box('Stop',fmt$(b.stop_premium),'neg')}
   ${box('Contracts',b.contracts_n+' × '+fmt$(b.cost))}
   ${box('OTM',b.otm+'%')}
   ${box('Spread / OI',b.spread_pct+'% · '+b.oi.toLocaleString())}
   <div class="dbox"><span>Confidence</span><b style="color:${b.conf_band==='A'?css('--up'):b.conf_band==='B'?'#4ea8de':b.conf_band==='C'?css('--warn'):css('--down')}">${b.conf} · ${b.conf_band}</b></div>
  </div>${alt}`:''}
  ${near}
  <div class="sig flow" style="margin-top:10px;border-color:var(--warn)"><span><b>Read this before acting.</b> ${C?C.rule_note:''} Bid/ask/OI above are from the chain scan at <b>${C?C.scanned_at:'—'}</b> with SPY at ${C?fmt$(C.spot_at_scan):'—'}; "Est. now" is a Black-Scholes estimate off the live SPY quote and the scanned IV, <b>not a real quote</b>. Browser-side live option quotes are not available on the free data tiers.</span></div>
 </div>`;
}

function render(){
 document.getElementById('tabs').innerHTML=BOOKS.map(([id,n])=>{
  const c=id==='overview'?'':id==='spy'?'':id==='whale'?(D.whale&&D.whale.in_13f_window?'!':''):id==='closed'?D.closed.length:id==='options'?D.options.length:id==='journal'?(D.closed.length+D.closed_options.length):by(id).length;
  return`<button class="tab ${tab===id?'on':''}" data-t="${id}">${n}${c!==''?`<span class="n">${c}</span>`:''}</button>`;
 }).join('');
 document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{tab=b.dataset.t;render();});
 const v=document.getElementById('views');
 if(tab==='overview')v.innerHTML=overview();
 else if(tab==='spy')v.innerHTML=spyView();
 else if(tab==='whale')v.innerHTML=whaleView();
 else if(tab==='options')v.innerHTML=optionsView();
 else if(tab==='journal')v.innerHTML=journalView();
 else if(tab==='closed')v.innerHTML=closedView();
 else v.innerHTML='<div class="panel">'+posTable(by(tab),{dir:tab==='vol24',tgt:tab!=='vol24'})+'</div>';
 document.querySelectorAll('.sortable th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(!k)return;if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=-1;}render();});
 document.getElementById('foot').innerHTML='Paper portfolio — $1,000 total across the stock books, weighted by conviction score within each book; exits return cash to the book. Options are a separate pool, max $250 per trade. No fees/slippage. Scores and 12m bull/base/bear targets are fixed at flag time and never revised; 3m/6m targets are √-time interpolations of the 12m range. Alpha = return minus same-day SPY. 24h-volatility direction calls are graded automatically after 5 trading days; the hit rate is shown un-cherry-picked and the feature dies if it can’t beat a coin flip over 30 calls. Penny book is listed-exchange only, $0.50–$5.00. Long-term book uses written criteria (founder-led, category creator, large TAM) — with the stated caveat that "find the next Tesla" carries survivorship bias. Prices via Yahoo Finance. Research tool, not investment advice.';
}
setStamp();render();
liveTick();setInterval(liveTick,60000);
window.addEventListener('resize',render);
</script></body></html>
'''

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
