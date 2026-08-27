# RESET MANIFEST — scanner-terminal-research-reset.v1

**Date/time:** 2026-08-27 (UTC)
**Repository:** amercado19/scanner-terminal
**Source commit SHA (pre-reset HEAD):** `2ec6fb68109fc545ccfb16fea16609e910db0b44`
**Migration id:** `scanner-terminal-research-reset.v1`

## Why the reset happened
The repository previously served a **paper-trading measurement instrument** (five books, conviction
scores, direction calls, SPY-alpha, 0DTE/1DTE trades) as the primary experience. The owner migrated
the primary experience to the **Structural Research Scanner** — a non-predictive structural screen of
long calls and long puts — and reset the legacy paper-trading system out of active operation. The old
system is preserved read-only here; it is no longer the landing page and no longer auto-generates data.

## What was archived (exact pre-reset copies, this folder)
- `ledger.json` — full pre-reset paper-trading ledger (positions, options_positions, closed_options, history, spy_state, etc.)
- `scanner_dashboard.html` — the rendered legacy dashboard (read-only snapshot)
- `build_dashboard.py` — the legacy engine
- `README.md` — the pre-reset README
- `workflows/worker.yml`, `workflows/nightly_scan.yml`, `workflows/intraday-exits.yml` — the legacy scheduled workflows (as they were, with schedules enabled)

**No legacy evidence was deleted.** Git history also retains everything at commit `2ec6fb68…`.

## What changed in active operation
- **Active `ledger.json` reset to zero** — all positions/options/closed/history emptied; `meta` preserved with a `reset` marker. No executed trades unless a real trade is manually added later.
- **Legacy workflows disabled** — `worker.yml`, `nightly_scan.yml`, `intraday-exits.yml` had their `schedule:` (cron) blocks removed; each keeps `workflow_dispatch:` only, so they run **manual-only** and never auto-populate the active dashboard.
- **Active automated workflow:** `.github/workflows/research-scanner.yml` (unchanged) — the only scheduled writer; writes ONLY `research-scanner/data/research_watchlist.json`.
- **New unified landing:** root `index.html` (the Scanner Terminal, research default) + `.nojekyll`. Canonical data source: `research-scanner/data/research_watchlist.json`.

## How to restore the legacy system (reversible)
1. Restore the legacy files from this archive (or `git checkout 2ec6fb68 -- ledger.json scanner_dashboard.html build_dashboard.py .github/workflows/worker.yml .github/workflows/nightly_scan.yml .github/workflows/intraday-exits.yml`).
2. Re-add the `schedule:` cron blocks to the three workflows (originals in `workflows/` here).
3. Remove root `index.html` (or repoint Pages) if you want the legacy dashboard as the landing again.
4. The Research Scanner module (`research-scanner/`) is independent and can remain regardless.

Restoration is non-destructive to the Research Scanner state; the two systems share no files.
