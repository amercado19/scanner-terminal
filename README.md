# Scanner Terminal — paper-trading measurement instrument

Five books of paper positions, benchmarked against SPY, with automatic exits and
un-cherry-picked scorekeeping. **No real money. Not investment advice.** Built in
Claude Cowork (Trading Bot project); the living copy updates every weekday at
5:15pm ET via a scheduled cloud task — this repo is a snapshot of the code + state.

## Files
- `build_dashboard.py` — the whole engine: fetches prices (Yahoo), options quotes
  (CBOE delayed), benchmarks vs same-day SPY, enforces stock stops, grades
  bull/bear direction calls after 5 trading days, marks option contracts at bid
  and exits at target (2x ask) / stop (0.5x ask) / 2-day clock / expiry, then
  renders the self-contained dashboard.
- `ledger.json` — all state: positions per book (core / vol24 / options / penny /
  longterm), conviction scores, targets, closed trades, daily history. (Spark
  price caches trimmed for the repo; they rebuild on the first run.)
- `scanner_dashboard.html` — NOT committed; it is generated output. Run the
  command below to produce it, then open it in any browser.

## Run
```
python3 build_dashboard.py ledger.json scanner_dashboard.html
```
Python 3.8+, no third-party packages. Refreshes prices and rewrites the dashboard.
Note: if you run it locally AND the cloud task runs, the two ledgers diverge —
the cloud copy in the Claude project is the source of truth.

## Honesty rules (short version)
Scores, targets, stops and direction calls are fixed at flag time and never
revised. Alpha is measured against buying SPY the same day. Each speculative book
carries a pre-registered kill rule (direction calls: dead if <=50% hit rate after
30; options: dead if net negative after 20). Full methodology lives in the
Trading Bot project docs.
