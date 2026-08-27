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
- `scanner_dashboard.html` — the rendered terminal, exact copy from the cloud
  scanner with the stock-theme icon embedded. Open in any browser; regenerate
  any time with the command below.
- `icon_preview.png` — the app icon (candlesticks + trend arrow).

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

---

## Structural Research Scanner (separate module — research only)

A **non-predictive** structural screen that lives in [`research-scanner/`](research-scanner/) and is
**logically separate** from the paper-trading engine above. It reduces a curated universe to the
**long calls and long puts** that objectively pass frozen structural filters (premium $0.75–$3.00,
max cost $300, DTE 21–60, open interest ≥ 1,000, volume ≥ 100, bid/ask ≤ 10% of mid, theta burden
≤ 3%/day), keeps a persistent watchlist with per-contract history, and publishes a page.

It **never** ranks, scores, predicts direction, estimates return or win probability, or recommends a
trade — and it never touches `ledger.json`, `build_dashboard.py`, or the conviction/direction/0DTE
logic. It reuses only the repo's free CBOE-delayed data technique. Scheduled by
`.github/workflows/research-scanner.yml`, independent of the other workflows.

**Live page:** [research-scanner/](https://amercado19.github.io/scanner-terminal/research-scanner/) ·
**Run locally:** `python3 research-scanner/research_scanner.py --provider sample`
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
- `scanner_dashboard.html` — the rendered terminal, exact copy from the cloud
  scanner with the stock-theme icon embedded. Open in any browser; regenerate
  any time with the command below.
- `icon_preview.png` — the app icon (candlesticks + trend arrow).

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
