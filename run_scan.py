#!/usr/bin/env python3
"""Daily scan driver: SPY model -> grade prior calls -> option funnel -> pick top 8
-> whale feed -> write ledger. Gates and weights are read-only here."""
import json, re, sys, time, urllib.request, datetime
import option_score as OS
import option_scan as SC
import spy_model as SM

# ---- dynamic rolling expiries: this-week / next-week / two-weeks Fridays ----
TODAY_D = datetime.date.today()
TODAY = TODAY_D.isoformat()


def _add_bdays(d, n):
    while n > 0:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _next_friday(d):
    return d + datetime.timedelta(days=(4 - d.weekday()) % 7)  # Fri=4; today if today is Fri


def _exp(d):
    return (d.strftime('%y%m%d'), d.isoformat())


THIS_WEEK_FRIDAY = _next_friday(TODAY_D)                            # 0-7 DTE
NEXT_WEEK_FRIDAY = THIS_WEEK_FRIDAY + datetime.timedelta(days=7)    # 7-14 DTE
TWO_WEEKS_FRIDAY = THIS_WEEK_FRIDAY + datetime.timedelta(days=14)   # 14-21 DTE
WEEKLIES = [_exp(THIS_WEEK_FRIDAY), _exp(NEXT_WEEK_FRIDAY), _exp(TWO_WEEKS_FRIDAY)]

NEXT_SESSION = _add_bdays(TODAY_D, 1).isoformat()
EXIT_2D = _add_bdays(TODAY_D, 2).isoformat()      # two trading days from today
DTE1 = _exp(_add_bdays(TODAY_D, 1))               # SPY 1DTE = next trading day
DELTA_LO, DELTA_HI = 0.30, 0.50                   # delta targeting band
# Hard delta-gating empties Next/Two-Weeks at the $300 cap: their 0.30-0.50 strikes
# cost >$300, and the affordable strikes are <0.30 delta. So by default delta is a
# SCORING signal (attached to each candidate + edge score), and the OTM band selects,
# which keeps all three weeks populated. Set USE_DELTA_GATE=True to hard-filter to
# 0.30-0.50 delta instead (accepts sparser Next/Two-Weeks buckets).
USE_DELTA_GATE = False
DGATE = (DELTA_LO, DELTA_HI) if USE_DELTA_GATE else (None, None)
print(f"expiry window: this={WEEKLIES[0][1]} next={WEEKLIES[1][1]} two={WEEKLIES[2][1]} | delta_gate={USE_DELTA_GATE}")

L = json.load(open('ledger.json'))
META = L['meta']

# ---------------------------------------------------------------- SPY model
m = SM.spy_direction()
m['parts']['news'] = SM.news_score(META.get('signals', []))
m['score'] = sum(v for v in m['parts'].values() if v is not None)
m['bias'] = 'BULLISH' if m['score'] >= 60 else ('BEARISH' if m['score'] <= 40 else 'NEUTRAL')
print(f"SPY {m['price']} bias={m['bias']} score={m['score']} parts={m['parts']} day={m['day_pct']}%")

# grade any prior call whose target session has already closed
spy_hist = SM.yq('SPY', '1mo')['closes']
u = 'https://query1.finance.yahoo.com/v8/finance/chart/SPY?range=1mo&interval=1d'
raw = json.load(urllib.request.urlopen(urllib.request.Request(u, headers=SM.UA), timeout=25))
res = raw['chart']['result'][0]
bars = [(datetime.datetime.utcfromtimestamp(t).strftime('%Y-%m-%d'), c)
        for t, c in zip(res['timestamp'], res['indicators']['quote'][0]['close']) if c]
bar = dict(bars)
dates = [d for d, _ in bars]

for c in L.get('spy_calls', []):
    if c.get('outcome') or c['bias'] == 'NEUTRAL':
        continue
    later = [d for d in dates if d > c['date']]
    if not later:
        continue
    tgt = later[0]
    if tgt >= TODAY:      # session not closed yet — stays pending, no peeking
        continue
    ret = 100 * (bar[tgt] / c['spy_close'] - 1)
    c['next_ret'] = round(ret, 2)
    c['outcome'] = 'hit' if ((c['bias'] == 'BULLISH') == (ret > 0)) else 'miss'
    print(f"  graded {c['date']} {c['bias']} -> {tgt} {ret:+.2f}% {c['outcome']}")

if not any(c['date'] == TODAY for c in L.get('spy_calls', [])):
    L.setdefault('spy_calls', []).append({
        'date': TODAY, 'bias': m['bias'], 'score': m['score'], 'parts': m['parts'],
        'spy_close': round(m['price'], 2), 'next_ret': None, 'outcome': None})
L['spy_state'] = {k: m[k] for k in ('price', 'sma20', 'sma50', 'ret5', 'vix', 'vix_chg5',
                                    'breadth_pct', 'parts', 'day_pct', 'score', 'bias')}
META['whale'] = SM.whale_windows(TODAY_D)

# --------------------------------------------------------------- funnel
tier_model = 'model_strong' if (m['score'] >= 80 or m['score'] <= 20) else (
    'model' if (65 <= m['score'] <= 79 or 21 <= m['score'] <= 35) else 'none')
direction = 'bull' if m['bias'] == 'BULLISH' else 'bear'

MOVERS = [  # verified catalysts checked against Finnhub company-news this morning
    ('RDDT', 'catalyst_hard', 'S&P 500 inclusion announced 8/13, effective before Tuesday’s open — index funds are forced buyers'),
    ('NU',   'catalyst_hard', 'Q2 earnings: first billion-dollar quarter, +13.9% on 57M shares'),
    ('SNDK', 'catalyst_hard', 'Investor day, new long-term profit targets — NAND/AI storage repricing, +8.4%'),
    ('LUNR', 'catalyst_hard', 'Q2 earnings, $1B revenue goal reaffirmed, +11.1%'),
    ('HTFL', 'catalyst_hard', 'Q2 earnings, +30.9% — largest single-day mover on the tape'),
    ('AEHR', 'catalyst_hard', '$22M follow-on wafer-level AI test order, +10.5%'),
    ('CAPR', 'catalyst_hard', 'Q2 earnings, +69.6% — biotech, thinnest chain of the group'),
]

near = []
pool = []

# 1) SPY 1DTE — exactly one strike, its own band
rv_spy = OS.realized_vol_from_closes(SC.closes('SPY'))
dte = SC.candidates('SPY', direction, tier_model, DTE1[0], TODAY, DTE1[1], DTE1[1],
                    otm_lo=0.2, otm_hi=1.5, prem_lo=0.30, prem_hi=0.60, rv=rv_spy,
                    top=99, near_out=near)
print(f'1DTE SPY candidates passing gates: {len(dte)}')
dte_pick = dte[0] if dte else None
if dte_pick:
    dte_pick['feeder'] = '1dte'
    dte_pick['why'] = (f"1dte: SPY composite {m['bias']} {m['score']}/100 — trend {m['parts']['trend']}/30, "
                       f"VIX {m['vix']}, breadth {m['breadth_pct']}%")
    pool.append(dte_pick)

# 2) ETF universe — scanned across all three weekly expiries
for t in SC.ETF_UNIVERSE:
    if t == 'SPY':
        continue
    rv = OS.realized_vol_from_closes(SC.closes(t))
    for yy, iso in WEEKLIES:
        for c in SC.candidates(t, direction, tier_model, yy, TODAY, EXIT_2D, iso,
                               rv=rv, top=1, near_out=near,
                               delta_lo=DGATE[0], delta_hi=DGATE[1]):
            c['feeder'] = 'etf'
            c['why'] = f"etf {iso}: SPY composite {m['bias']} {m['score']} — index-level expression of the model call"
            pool.append(c)
    time.sleep(0.3)

# 3) blue chips — thematic alignment only, across all three weekly expiries
for t in SC.BLUECHIP:
    rv = OS.realized_vol_from_closes(SC.closes(t))
    for yy, iso in WEEKLIES:
        for c in SC.candidates(t, direction, 'catalyst_soft', yy, TODAY, EXIT_2D, iso,
                               rv=rv, top=1, near_out=near,
                               delta_lo=DGATE[0], delta_hi=DGATE[1]):
            c['feeder'] = 'bluechip'
            c['why'] = f'bluechip {iso}: thematic alignment with an active world signal, no company-specific event'
            pool.append(c)
    time.sleep(0.3)

# 4) catalyst movers — across all three weekly expiries
for t, tier, why in MOVERS:
    rv = OS.realized_vol_from_closes(SC.closes(t))
    for yy, iso in WEEKLIES:
        for c in SC.candidates(t, 'bull', tier, yy, TODAY, EXIT_2D, iso,
                               rv=rv, top=1, near_out=near,
                               delta_lo=DGATE[0], delta_hi=DGATE[1]):
            c['feeder'] = 'mover'
            c['why'] = f'mover {iso}: {why}'
            pool.append(c)
    time.sleep(0.3)

print(f'total candidates clearing gates: {len(pool)}  |  near-misses logged: {len(near)}')

# --------------------------------------------------------------- selection
# Balance the picks ACROSS the three weekly expiries so the dashboard's
# This Week / Next Week / Two Weeks Out tabs all populate. Near-dated
# contracts almost always out-score far-dated ones (less time value to pay,
# tighter breakeven), so a plain global top-N by score starves the later two
# weeks — which is exactly why "Next Week" showed empty. Instead we bucket the
# pool by expiry and round-robin the buckets, taking the best-scoring unused
# ticker from each week on every pass until the daily cap is filled.
CAP = META.get('options_daily_cap', 8)
WEEK_ISOS = [iso for _, iso in WEEKLIES]
buckets = {}
for c in pool:
    buckets.setdefault(c['expiry'], []).append(c)
for iso in buckets:
    buckets[iso].sort(key=lambda c: -c['score']['total'])
# weekly Fridays first (in chronological order), then any stragglers
order = [iso for iso in WEEK_ISOS if iso in buckets]
order += [iso for iso in buckets if iso not in WEEK_ISOS]

picked, seen = [], set()
if dte_pick:
    picked.append(dte_pick); seen.add('SPY')
idx = {iso: 0 for iso in order}
progressed = True
while len(picked) < CAP and progressed:
    progressed = False
    for iso in order:
        if len(picked) >= CAP:
            break
        b = buckets.get(iso, [])
        while idx[iso] < len(b):
            c = b[idx[iso]]; idx[iso] += 1
            if c['underlying'] in seen:
                continue
            seen.add(c['underlying']); picked.append(c); progressed = True
            break

print(f'selected {len(picked)}/{CAP}:')
for c in picked:
    print(f"  {c['underlying']:5s} ${c['strike']:<8g} {c['type']:4s} ask {c['ask']:<6} "
          f"otm {c['otm']:<6} spr {c['spread_pct']:<6} oi {c['oi']:<8} n {c['contracts_n']} "
          f"cost ${c['cost']:<7} conf {c['score']['total']}{c['score']['band']} [{c['feeder']}]")

for c in picked:
    exp = c['expiry'].replace('-', '')[2:]
    k = f"{int(round(c['strike'] * 1000)):08d}"
    L['options_positions'].append({
        'contract': f"{c['underlying']}{exp}{'C' if c['type'] == 'call' else 'P'}{k}",
        'underlying': c['underlying'], 'type': c['type'], 'strike': c['strike'],
        'expiry': c['expiry'], 'source_call': c['why'], 'entry_date': TODAY,
        'entry_ask': c['ask'], 'entry_bid': c['bid'], 'contracts_n': c['contracts_n'],
        'cost': c['cost'], 'exit_by': c['exit_by'], 'status': 'open', 'oi': c['oi'],
        'iv': c['iv'], 'entry_spot': c['spot'],
        'note': (f"{c['otm']}% OTM, {c['spread_pct']}% spread, OI {c['oi']}. "
                 f"Breakeven ratio {c['score']['detail']['be_ratio']} — needs "
                 f"{c['score']['detail']['required_move_pct']}% vs "
                 f"{c['score']['detail']['expected_move_pct']}% implied over the hold."),
        'target_premium': round(c['ask'] * 6, 2), 'stop_premium': round(c['ask'] * 0.5, 3),
        'conf': c['score']['total'], 'conf_band': c['score']['band'],
        'conf_parts': c['score']['parts'], 'conf_detail': c['score']['detail'],
    })

# live-entry panel for the SPY 1DTE
spy_near = [n for n in near if n['underlying'] == 'SPY'][:8]
L['spy_entry_candidate'] = {
    'scanned_at': datetime.datetime.now(datetime.timezone.utc).astimezone(
        __import__('zoneinfo').ZoneInfo('America/New_York')).strftime('%b %d, %Y · %I:%M %p ET').replace(' 0', ' '),
    'spot_at_scan': round(m['price'], 2), 'expiry': DTE1[1], 'type': 'call' if direction == 'bull' else 'put',
    'bias': m['bias'], 'score': m['score'], 'realized_vol': round(rv_spy, 3) if rv_spy else None,
    'best': ({'strike': dte_pick['strike'], 'otm': dte_pick['otm'], 'bid': dte_pick['bid'],
              'ask': dte_pick['ask'], 'spread_pct': dte_pick['spread_pct'], 'oi': dte_pick['oi'],
              'iv': dte_pick['iv'], 'contracts_n': dte_pick['contracts_n'], 'cost': dte_pick['cost'],
              'conf': dte_pick['score']['total'], 'conf_band': dte_pick['score']['band'],
              'conf_parts': dte_pick['score']['parts'], 'conf_detail': dte_pick['score']['detail'],
              'target_premium': round(dte_pick['ask'] * 6, 2),
              'stop_premium': round(dte_pick['ask'] * 0.5, 3)} if dte_pick else None),
    'passing': [{'strike': c['strike'], 'otm': c['otm'], 'bid': c['bid'], 'ask': c['ask'],
                 'spread_pct': c['spread_pct'], 'oi': c['oi'], 'iv': c['iv'],
                 'contracts_n': c['contracts_n'], 'cost': c['cost'],
                 'conf': c['score']['total'], 'conf_band': c['score']['band'],
                 'conf_parts': c['score']['parts'], 'conf_detail': c['score']['detail'],
                 'target_premium': round(c['ask'] * 6, 2),
                 'stop_premium': round(c['ask'] * 0.5, 3)} for c in dte],
    'near_misses': [{k: v for k, v in n.items() if k != 'underlying'} for n in spy_near],
    'informational': True,
    'rule_note': (f"Today's one-strike 1DTE slot {'fired (see above) and is now tracked' if dte_pick else 'did not fire — no strike cleared the gates'}. "
                  f"{len(picked)} of {CAP} daily contract slots used. This panel is a LIVE ENTRY CHECK only; "
                  f"nothing here is logged twice, because discretionary same-day re-entries would corrupt "
                  f"the 20-contract sample the book exists to measure."),
}

json.dump(L, open('ledger.json', 'w'), indent=1)
print('ledger updated.')
