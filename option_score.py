"""Option confidence score — 0-100, five components, FROZEN 2026-08-13.

The point of this file is falsifiability, not decoration. Every component is
computed from data available AT ENTRY and is never recomputed afterwards, so
the score can be graded against realized outcomes. Weights must NEVER be
re-tuned after seeing which contracts worked.

KILL RULE: after 20 resolved contracts, compare mean P/L% of band A+B against
band C+D. If A+B does not beat C+D, the score carries no information and this
entire file is deleted from the pipeline. Recorded, not quietly dropped.

Components
  execution   /25  spread % of ask (15) + open interest (10)
  breakeven   /25  required move to breakeven vs expected move over the hold
  signal      /25  strength of the directional thesis, by registered tier
  clock       /15  residual time value remaining at the planned exit date
  iv_cost     /10  implied vol vs the underlying's own 20-day realized vol

Bands: A >=75, B 60-74, C 45-59, D <45.
"""

import math
from datetime import date

BANDS = [(75, 'A'), (60, 'B'), (45, 'C'), (0, 'D')]
MAX_HV_RANK = 75.0  # HV30-rank above this is flagged as IV-crush risk in the edge score

SIGNAL_TIERS = {
    # tier -> (points, what qualifies)
    'model_strong':   (25, 'SPY composite >=80 or <=20 — the model is at an extreme'),
    'model':          (18, 'SPY composite 65-79 or 21-35 — model leans but not extreme'),
    'catalyst_hard':  (22, 'Verified hard catalyst: earnings surprise, signed contract, M&A, FDA'),
    'catalyst_soft':  (13, 'Thematic alignment with an active world signal, no company-specific event'),
    'control':         (5, 'Deliberately catalyst-free — trades as a control against a catalyst name'),
    'none':            (3, 'No articulable directional thesis'),
}


def _band(total):
    for cut, letter in BANDS:
        if total >= cut:
            return letter
    return 'D'


def _dcount(a, b):
    """Calendar days between two YYYY-MM-DD strings."""
    ya, ma, da = (int(x) for x in a.split('-'))
    yb, mb, db = (int(x) for x in b.split('-'))
    return (date(yb, mb, db) - date(ya, ma, da)).days


def score_contract(*, spot, strike, opt_type, ask, bid, oi, iv,
                   entry_date, exit_by, expiry, signal_tier,
                   realized_vol=None, hv_rank=None, delta=None, vol_oi_ratio=None):
    """Return {'total', 'band', 'parts', 'detail', 'edge', 'edge_flags'}.

    'total'/'band'/'parts' are the FROZEN, pre-registered 0-100 score — never re-tune.
    'edge' is a SEPARATE, non-frozen signal (low HV-rank / in-band delta / volume surge)
    that is intentionally NOT summed into 'total' so the falsifiability guarantee holds.
    """
    parts, detail = {}, {}

    # --- execution /25 -----------------------------------------------------
    spr = (ask - bid) / ask * 100 if ask > 0 else 100
    if spr <= 2:    e_spr = 15
    elif spr <= 5:  e_spr = 12
    elif spr <= 10: e_spr = 8
    elif spr <= 15: e_spr = 4
    elif spr <= 20: e_spr = 1
    else:           e_spr = 0
    if oi >= 10000:  e_oi = 10
    elif oi >= 5000: e_oi = 8
    elif oi >= 2000: e_oi = 6
    elif oi >= 1000: e_oi = 4
    elif oi >= 500:  e_oi = 2
    else:            e_oi = 0
    parts['execution'] = e_spr + e_oi
    detail['spread_pct'] = round(spr, 1)
    detail['oi'] = oi

    # --- breakeven /25 -----------------------------------------------------
    # How far the underlying must travel to break even at expiry, measured in
    # units of the move the option market itself implies over the holding
    # period. Above 1.0 means you are paying for a move larger than the one
    # being priced -- the single most reliable way to lose on long premium.
    if opt_type == 'call':
        req = (strike + ask) - spot
    else:
        req = spot - (strike - ask)
    req_pct = max(req, 0) / spot * 100
    hold_days = max(_dcount(entry_date, exit_by), 1)
    exp_pct = (iv * math.sqrt(hold_days / 365.0)) * 100 if iv else 0.01
    ratio = req_pct / exp_pct if exp_pct > 0 else 99
    if ratio <= 0.75:   b = 25
    elif ratio <= 1.00: b = 20
    elif ratio <= 1.25: b = 15
    elif ratio <= 1.50: b = 10
    elif ratio <= 2.00: b = 5
    else:               b = 0
    parts['breakeven'] = b
    detail['required_move_pct'] = round(req_pct, 2)
    detail['expected_move_pct'] = round(exp_pct, 2)
    detail['be_ratio'] = round(ratio, 2)

    # --- signal /25 --------------------------------------------------------
    parts['signal'] = SIGNAL_TIERS.get(signal_tier, SIGNAL_TIERS['none'])[0]
    detail['signal_tier'] = signal_tier

    # --- clock /15 ---------------------------------------------------------
    # Days of life remaining on the contract at the PLANNED exit. Zero means
    # the plan is to hold to expiry, where all remaining time value is gone
    # and only intrinsic value can save the trade.
    resid = max(_dcount(exit_by, expiry), 0)
    if resid == 0:    c = 3
    elif resid <= 3:  c = 8
    elif resid <= 9:  c = 13
    else:             c = 15
    parts['clock'] = c
    detail['days_left_at_exit'] = resid

    # --- iv cost /10 -------------------------------------------------------
    if realized_vol and realized_vol > 0:
        ivr = iv / realized_vol
        if ivr <= 1.0:   v = 10
        elif ivr <= 1.2: v = 8
        elif ivr <= 1.5: v = 5
        elif ivr <= 2.0: v = 2
        else:            v = 0
        detail['iv_vs_realized'] = round(ivr, 2)
    else:
        v = 5  # unknown realized vol -> neutral, and say so
        detail['iv_vs_realized'] = None
    parts['iv_cost'] = v

    # --- edge signals (SEPARATE from the frozen total; never summed into it) -------
    edge, eflags = 0, []
    if hv_rank is not None:
        if hv_rank <= 35:
            edge += 10; eflags.append(f'low HV-rank {hv_rank}')
        elif hv_rank >= MAX_HV_RANK:
            edge -= 10; eflags.append(f'high HV-rank {hv_rank} (crush risk)')
    if delta is not None and 0.30 <= abs(delta) <= 0.55:
        edge += 10; eflags.append(f'delta {abs(delta):.2f} in band')
    if vol_oi_ratio is not None and vol_oi_ratio >= 2.0:
        edge += 10; eflags.append(f'vol surge {vol_oi_ratio}x OI')
    detail['iv_rank_proxy'] = hv_rank
    detail['delta'] = round(delta, 3) if delta is not None else None
    detail['vol_oi_ratio'] = vol_oi_ratio

    total = sum(parts.values())
    return {'total': total, 'band': _band(total), 'parts': parts, 'detail': detail,
            'edge': edge, 'edge_flags': eflags}


def realized_vol_from_closes(closes, window=20):
    """Annualized close-to-close realized vol from a list of closes."""
    c = [x for x in closes if x][-(window + 1):]
    if len(c) < 6:
        return None
    rets = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def grade_bands(closed_options):
    """Band-vs-outcome table. This is what decides whether the score survives."""
    out = {}
    for o in closed_options:
        b = o.get('conf_band')
        if not b or o.get('pl_pct') is None:
            continue
        d = out.setdefault(b, {'n': 0, 'sum_pl_pct': 0.0, 'wins': 0, 'sum_pl': 0.0})
        d['n'] += 1
        d['sum_pl_pct'] += o['pl_pct']
        d['sum_pl'] += o.get('pl', 0)
        if o['pl_pct'] > 0:
            d['wins'] += 1
    for b, d in out.items():
        d['avg_pl_pct'] = round(d['sum_pl_pct'] / d['n'], 1)
        d['win_rate'] = round(d['wins'] / d['n'] * 100, 1)
        d['sum_pl'] = round(d['sum_pl'], 2)
    return out


def kill_check(closed_options):
    """Returns (verdict, message). Pre-registered: A+B must beat C+D by 20 resolved."""
    g = grade_bands(closed_options)
    n = sum(d['n'] for d in g.values())
    if n < 20:
        return 'pending', f'{n}/20 resolved contracts scored — too few to judge the score.'
    hi = [d for b, d in g.items() if b in ('A', 'B')]
    lo = [d for b, d in g.items() if b in ('C', 'D')]
    if not hi or not lo:
        return 'pending', 'Need resolved contracts in both the A/B and C/D bands to compare.'
    hi_avg = sum(d['sum_pl_pct'] for d in hi) / sum(d['n'] for d in hi)
    lo_avg = sum(d['sum_pl_pct'] for d in lo) / sum(d['n'] for d in lo)
    if hi_avg > lo_avg:
        return 'alive', f'A/B avg {hi_avg:.1f}% vs C/D avg {lo_avg:.1f}% — score discriminates. Keep.'
    return 'dead', (f'A/B avg {hi_avg:.1f}% vs C/D avg {lo_avg:.1f}% — the score carries no '
                    f'information. Per the pre-registered rule it is now deleted from the pipeline.')
