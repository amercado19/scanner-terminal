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


MIN_DTE = 2   # STRUCTURAL FLOOR (matches option_scan.MIN_DTE): skip 0-1 DTE entirely.
# Build the three weekly expiries as the next three Fridays that are ALREADY >= MIN_DTE
# away. On a Thursday/Friday the imminent Friday (0-1 DTE) is skipped and the window
# rolls forward, so the board never carries a same-week gamma trap. On Mon-Wed the
# current-week Friday still qualifies (2-4 DTE) and leads the window as before.
_fri = _next_friday(TODAY_D)
if (_fri - TODAY_D).days < MIN_DTE:
    _fri = _fri + datetime.timedelta(days=7)
WEEKLIES = [_exp(_fri + datetime.timedelta(days=7 * i)) for i in range(3)]

NEXT_SESSION = _add_bdays(TODAY_D, 1).isoformat()
EXIT_2D = _add_bdays(TODAY_D, 2).isoformat()      # two trading days from today
# SPY 1DTE paper feeder is retired from the default funnel: 0-1 DTE is a structural
# exclusion (see MIN_DTE). Intraday 0DTE execution lives in the dedicated live SPY
# terminal, not this once/twice-a-day paper board. Flip to True only to re-enable it.
SCAN_1DTE_SPY = False
DTE1 = _exp(_add_bdays(TODAY_D, 1))               # kept for the optional feeder below
DELTA_LO, DELTA_HI = 0.30, 0.50                   # delta targeting band
# Hard delta-gating empties Next/Two-Weeks at the $300 cap: their 0.30-0.50 strikes
# cost >$300, and the affordable strikes are <0.30 delta. So by default delta is a
# SCORING signal (attached to each candidate + edge score), and the OTM band selects,
# which keeps all three weeks populated. Set USE_DELTA_GATE=True to hard-filter to
# 0.30-0.50 delta instead (accepts sparser Next/Two-Weeks buckets).
USE_DELTA_GATE = False
DGATE = (DELTA_LO, DELTA_HI) if USE_DELTA_GATE else (None, None)

# ---- correlation cap (post-mortem finding) ---------------------------------
# In the first 19 closed trades, 12 of 16 losers were the SAME trade: "SPY is
# BULLISH -> buy QQQ / IWM / NVDA / PLTR / AAPL", i.e. long US-equity beta held
# eight ways. When SPY didn't rally they all lost together. The board looked
# diversified (8 tickers) but was one bet. The winners were the UNCORRELATED
# names (XLE energy on a Venezuela-oil catalyst). So cap how many positions can
# share the broad-beta driver, forcing the board toward different drivers.
CORRELATION_CAP = 3                        # max broad-US-beta positions on the board
BROAD_BETA = {'SPY', 'QQQ', 'IWM', 'DIA'}  # whole-market ETFs = pure beta


def _driver(c):
    """Coarse driver bucket for correlation control. Broad-market ETFs and generic
    'thematic alignment' blue chips are all long-US-beta and share ONE bucket; sector
    ETFs and catalyst movers each get their own idiosyncratic bucket."""
    t = c.get('underlying')
    if t in BROAD_BETA:
        return 'US_BETA'
    if c.get('feeder') == 'bluechip':      # picked on 'SPY bullish' thematic beta, no catalyst
        return 'US_BETA'
    if c.get('feeder') == 'etf':           # sector ETF (XLE/XLF/XLK/SMH): own driver
        return f'SECTOR:{t}'
    return f'NAME:{t}'                      # mover with a real company catalyst: idiosyncratic


print(f"expiry window: this={WEEKLIES[0][1]} next={WEEKLIES[1][1]} two={WEEKLIES[2][1]} | delta_gate={USE_DELTA_GATE} | corr_cap={CORRELATION_CAP}")

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

# 1) SPY 1DTE — retired from the default funnel (0-1 DTE structural exclusion).
dte = []
dte_pick = None
rv_spy = OS.realized_vol_from_closes(SC.closes('SPY'))   # also used by the SPY entry panel below
if SCAN_1DTE_SPY:
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
else:
    print('1DTE SPY feeder: disabled (0-1 DTE structural exclusion)')

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
drivers = {}                                  # driver bucket -> count on the board
if dte_pick:
    picked.append(dte_pick); seen.add('SPY')
    drivers[_driver(dte_pick)] = drivers.get(_driver(dte_pick), 0) + 1
idx = {iso: 0 for iso in order}
corr_skipped = 0
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
            drv = _driver(c)
            # correlation cap: broad-beta bucket is limited; sector/catalyst buckets are not
            if drv == 'US_BETA' and drivers.get(drv, 0) >= CORRELATION_CAP:
                corr_skipped += 1
                continue
            seen.add(c['underlying']); picked.append(c)
            drivers[drv] = drivers.get(drv, 0) + 1
            progressed = True
            break

_beta_n = drivers.get('US_BETA', 0)
print(f'selected {len(picked)}/{CAP}: US_BETA={_beta_n}/{CORRELATION_CAP} cap, '
      f'{len(picked) - _beta_n} uncorrelated | {corr_skipped} beta-proxies skipped by corr-cap')
if len(picked) < CAP:
    print(f'  note: {CAP - len(picked)} slot(s) left EMPTY on purpose — not enough '
          f'uncorrelated setups to fill without over-concentrating in beta.')
for c in picked:
    print(f"  {c['underlying']:5s} ${c['strike']:<8g} {c['type']:4s} ask {c['ask']:<6} "
          f"otm {c['otm']:<6} spr {c['spread_pct']:<6} oi {c['oi']:<8} n {c['contracts_n']} "
          f"cost ${c['cost']:<7} conf {c['score']['total']}{c['score']['band']} [{c['feeder']}]")

# Dedup guard: never create a position that is ALREADY open. The intraday
# exit engine (build_dashboard.update_options) tracks trailing stops on open
# contracts and marks exited ones status='closed' — so we key on OPEN positions
# only. A contract that has exited (1DTE lock / 0DTE force-close / trailing stop)
# is no longer 'open', which frees that slot for a fresh setup tonight.
_open = [p for p in L['options_positions'] if p.get('status') == 'open']
_open_contracts = {p.get('contract') for p in _open}
_open_keys = {(p.get('underlying'), p.get('strike'), p.get('expiry')) for p in _open}
added = skipped = 0
for c in picked:
    exp = c['expiry'].replace('-', '')[2:]
    k = f"{int(round(c['strike'] * 1000)):08d}"
    contract_sym = f"{c['underlying']}{exp}{'C' if c['type'] == 'call' else 'P'}{k}"
    key = (c['underlying'], c['strike'], c['expiry'])
    if contract_sym in _open_contracts or key in _open_keys:
        skipped += 1
        continue
    _open_contracts.add(contract_sym); _open_keys.add(key)
    added += 1
    L['options_positions'].append({
        'contract': contract_sym,
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

print(f'options_positions: +{added} new, {skipped} already-open skipped '
      f'({len([p for p in L["options_positions"] if p.get("status") == "open"])} open total)')

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
