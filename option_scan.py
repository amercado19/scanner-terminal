"""Wide option scan — build the candidate funnel, score it, take the top N.

Gates are UNCHANGED from day one. This file only widens the funnel and ranks
what clears the gates by confidence.
"""
import json, time, urllib.request
import datetime as _dt
import option_score as OS

# ---- Options scanner criteria (tune here) ----------------------------------
BUDGET_CAP = 300     # max total $ per contract line (was 250)
DTE_MIN, DTE_MAX = 7, 14   # 1-2 weeks out: more runway, less immediate theta decay
MAX_IV_RANK_GATE = 75.0    # reject long buys when HV30 rank > 75% (IV-crush guard)
DELTA_LO, DELTA_HI = 0.30, 0.50   # dynamic delta targeting (ATM / near-OTM)

# NOTE: MAX_IV_RANK_GATE uses REALIZED-vol (HV30) rank, a proxy for IV rank. It flags
# names that have already been volatile; it does NOT reliably catch pre-event IV richness
# (a quiet stock with expensive options). For true crush protection see iv_richness
# (contract IV / HV30) attached to each candidate and scored as an edge signal.

_HV_CACHE = {}


def compute_realized_vol_rank(ticker_symbol):
    """30-day annualized realized vol (HV30) and its 52-week percentile rank, 0-100.
    Returns 50.0 as a safe neutral fallback on any failure."""
    try:
        import numpy as np
        import yfinance as yf
        df = yf.Ticker(ticker_symbol).history(period="1y3mo", interval="1d")
        if len(df) < 60:
            return 50.0
        logret = np.log(df["Close"] / df["Close"].shift(1))
        hv30 = logret.rolling(window=30).std() * np.sqrt(252)
        hv_52w = hv30.dropna().tail(252)
        if len(hv_52w) < 10:
            return 50.0
        cur, lo, hi = hv_52w.iloc[-1], hv_52w.min(), hv_52w.max()
        if hi - lo == 0:
            return 50.0
        return float(round((cur - lo) / (hi - lo) * 100.0, 1))
    except Exception as e:
        print(f"Warning: HV30 calc failed for {ticker_symbol}: {e}")
        return 50.0


def _hv_rank_cached(t):
    if t not in _HV_CACHE:
        _HV_CACHE[t] = compute_realized_vol_rank(t)
    return _HV_CACHE[t]


def atr_breakout(ticker_symbol, atr_mult=1.5, vol_mult=2.0):
    """Momentum trigger: latest daily candle range > atr_mult*ATR(14) AND
    volume > vol_mult*10-period average volume. Never raises."""
    try:
        import numpy as np
        import yfinance as yf
        df = yf.Ticker(ticker_symbol).history(period="3mo", interval="1d")
        if len(df) < 20:
            return {"breakout": False, "reason": "insufficient history"}
        h, l, c, v = df["High"], df["Low"], df["Close"], df["Volume"]
        tr = np.maximum(h - l, np.maximum((h - c.shift()).abs(), (l - c.shift()).abs()))
        atr = tr.rolling(14).mean().iloc[-1]
        rng = float(h.iloc[-1] - l.iloc[-1])
        avgvol = float(v.rolling(10).mean().iloc[-1])
        curvol = float(v.iloc[-1])
        breakout = bool(atr and rng > atr_mult * atr and avgvol and curvol > vol_mult * avgvol)
        return {"breakout": breakout, "atr_x": round(rng / atr, 2) if atr else None,
                "vol_x": round(curvol / avgvol, 2) if avgvol else None,
                "range": round(rng, 2), "atr14": round(float(atr), 2) if atr else None}
    except Exception as e:
        return {"breakout": False, "reason": str(e)}


def scan_breakouts(watchlist):
    """Return the subset of watchlist tickers currently in an ATR/volume breakout."""
    return [t for t in watchlist if atr_breakout(t).get("breakout")]

HDR = {'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'),
       'Accept': 'application/json', 'Referer': 'https://www.cboe.com/'}

ETF_UNIVERSE = ['SPY', 'QQQ', 'IWM', 'DIA', 'SMH', 'XLE', 'XLF', 'XLK']
BLUECHIP = ['AAPL', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'META', 'TSLA', 'TSM', 'AMD', 'AVGO',
            'NFLX', 'JPM', 'XOM', 'COIN', 'PLTR']

_CACHE = {}


def chain(t):
    if t in _CACHE:
        return _CACHE[t]
    url = f'https://cdn.cboe.com/api/global/delayed_quotes/options/{t}.json'
    last = None
    for i in range(4):
        try:
            d = json.load(urllib.request.urlopen(
                urllib.request.Request(url, headers=HDR), timeout=40))['data']
            _CACHE[t] = d
            return d
        except Exception as e:
            last = e
            time.sleep(10 * (i + 1))
    print(f'  !! {t} chain unavailable: {last}')
    _CACHE[t] = None
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
               otm_lo=2.0, otm_hi=10.0, prem_lo=0.05, prem_hi=None, rv=None, top=3,
               near_out=None, budget=None, dte_min=None, dte_max=None,
               apply_iv_gate=True, delta_lo=None, delta_hi=None):
    d = chain(t)
    if not d:
        return []
    budget = budget if budget is not None else BUDGET_CAP
    # DTE-window gate (all contracts here share expiry_iso, so check once)
    if dte_min is not None or dte_max is not None:
        try:
            dte = (_dt.date.fromisoformat(expiry_iso) - _dt.date.fromisoformat(entry)).days
        except Exception:
            dte = None
        lo = dte_min if dte_min is not None else 0
        hi = dte_max if dte_max is not None else 3650
        if dte is None or not (lo <= dte <= hi):
            if near_out is not None:
                near_out.append({'underlying': t, 'fail': f'DTE {dte} outside {lo}-{hi}'})
            return []
    # HV30-rank IV-crush guard: compute once per underlying (cached across feeders)
    hv_rank = _hv_rank_cached(t)
    if apply_iv_gate and hv_rank is not None and hv_rank > MAX_IV_RANK_GATE:
        if near_out is not None:
            near_out.append({'underlying': t, 'fail': f'HV30 rank {hv_rank} > {MAX_IV_RANK_GATE} (IV-crush guard)'})
        return []
    spot = d['current_price']
    want = 'C' if direction == 'bull' else 'P'
    out = []
    for o in d['options']:
        exp, cp, k = parse(o['option'])
        if exp != exp_yymmdd or cp != want:
            continue
        a, b, oi, iv = o['ask'], o['bid'], o['open_interest'], o['iv']
        delta = o.get('delta'); vol = o.get('volume') or 0
        otm = ((k - spot) / spot * 100) if cp == 'C' else ((spot - k) / spot * 100)
        # dynamic delta targeting when a band is requested and delta is available,
        # else fall back to the raw OTM band (keeps the 1DTE book + funnel alive)
        if delta_lo is not None and delta is not None:
            if not (delta_lo <= abs(delta) <= (delta_hi if delta_hi is not None else DELTA_HI)):
                continue
        elif not (otm_lo <= otm <= otm_hi):
            continue
        spr = (a - b) / a * 100 if a > 0 else 100
        vol_oi = round(vol / oi, 2) if oi else 0.0
        iv_rich = round(iv / rv, 2) if (rv and rv > 0 and iv) else None
        n = int(budget // (a * 100)) if a > 0 else 0
        fail = None
        if a <= 0 or a < prem_lo or (prem_hi and a > prem_hi):
            fail = f'premium ${a:.2f} outside ${prem_lo:.2f}-${prem_hi:.2f}' if prem_hi else f'premium ${a:.2f} unusable'
        elif oi < 500:
            fail = f'OI {oi} < 500'
        elif spr > 20:
            fail = f'spread {spr:.1f}% > 20%'
        elif n < 1 or n * a * 100 > budget:
            fail = f'cost gate: ask ${a:.2f} -> {n} contracts'
        if fail:
            if near_out is not None:
                near_out.append({'underlying': t, 'strike': k, 'otm': round(otm, 2),
                                 'ask': a, 'spread_pct': round(spr, 1), 'oi': oi, 'fail': fail})
            continue
        sc = OS.score_contract(spot=spot, strike=k, opt_type='call' if cp == 'C' else 'put',
                               ask=a, bid=b, oi=oi, iv=iv, entry_date=entry, exit_by=exit_by,
                               expiry=expiry_iso, signal_tier=tier, realized_vol=rv,
                               hv_rank=hv_rank, delta=delta, vol_oi_ratio=vol_oi)
        out.append({'underlying': t, 'spot': round(spot, 2), 'strike': k,
                    'type': 'call' if cp == 'C' else 'put', 'ask': a, 'bid': b, 'oi': oi,
                    'iv': iv, 'otm': round(otm, 2), 'spread_pct': round(spr, 1),
                    'contracts_n': n, 'cost': round(n * a * 100, 2),
                    'expiry': expiry_iso, 'exit_by': exit_by, 'tier': tier, 'score': sc,
                    'iv_rank_proxy': hv_rank, 'delta': delta, 'vol': vol,
                    'vol_oi_ratio': vol_oi, 'vol_surge': vol_oi >= 2.0, 'iv_richness': iv_rich})
    out.sort(key=lambda x: -x['score']['total'])
    return out[:top]
