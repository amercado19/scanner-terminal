# Real-time active-position tracker (Phase 2)

> **STATUS: SCAFFOLD — NOT OPERATIONAL.**
> The code here is complete and unit-tested against a mock provider, but it is **not**
> connected to a live real-time feed and **not** running anywhere. Until it is turned on
> (steps below), every open paper position is tracked by the **Phase 1** GitHub Actions
> scanner on **CBOE delayed** data, labelled as delayed throughout the dashboard.

## What this is

A persistent worker that watches **only the contracts with open paper positions** on a
**real-time** quote feed, evaluates the **same frozen stop policy as Phase 1** (imported
from `research_scanner.py` — the policy never forks), and writes an **append-only** event
log of raw observations, simulated exits, and descriptive per-trade analytics.

It exists because GitHub Actions cannot do this job: it is cron-scheduled, best-effort, capped
at ~15-minute granularity, and **must not hold a socket open**. So the two phases are split:

| | Phase 1 (live now) | Phase 2 (this folder) |
|---|---|---|
| Runs on | GitHub Actions cron (~15 min) | a persistent host |
| Scope | whole discovery universe | only open paper positions |
| Feed | CBOE **delayed** | any real-time provider (+ delayed fallback floor) |
| Data | `research-scanner/data/` | tracker's own append-only event store |

The worker **reads** the Phase 1 watchlist to learn which positions are open; it **never
writes back** into it. The two lifecycles stay independent and never race.

## Provider abstraction — the worker knows NO broker

The tracker engine is completely provider-agnostic. Providers are **swappable by configuration
only**; adding a broker is a **one-file** change and the worker + stop engine never change.

- `providers/base.py` — the `Provider` interface (`Quote`, `ContractRef`, modes) **and** a
  self-registration registry (`@register("name")`, `create_provider`, `available_providers`).
- Adapters self-register on import. Selectable today:
  - **`cboe`** — real, delayed feed (the honest fallback floor). No credential.
  - **`tradier`** — real, reference real-time adapter (REST polling).
  - **`polygon`, `alpaca`, `ibkr`, `schwab`** — registered **stubs**: selectable by config, but
    `get_quotes` raises `NotImplementedError` with the exact method + secret to fill in. They
    never fabricate quotes.
- `providers/template.py` — copy-me example for a brand-new adapter.
- **Robinhood** is intentionally absent — no official, supported market-data API for unattended
  use. Add `robinhood.py` the same way if one ever exists; the engine still won't change.

To add a provider: copy `template.py` to `providers/<name>.py`, implement `connect` + `get_quotes`
against the interface, add `@register("<name>")`. Nothing else changes.

## Configuration — the only place a provider is chosen

`config.py` resolves the provider by name (never in the worker). Precedence: CLI (`--provider`)
> env (`TRACKER_PROVIDER`) > `tracker.config.json` > default. Default is **`cboe`** (the honest
delayed floor) so nothing configured never silently pretends to be real-time.

```json
// tracker.config.json
{ "provider": "tradier", "fallback": "cboe", "allow_fallback": true }
```
```bash
export TRACKER_PROVIDER=tradier      # or set it in the file, or pass --provider tradier
```

## Honesty invariants (enforced by `test_tracker.py`, 61 checks)

- Never fabricates a quote. A missing/stale contract is `ok=false` with no price.
- Never evaluates stops on a `STALE` or `DISCONNECTED` feed.
- On a stop, exits at the **observed bid** (conservative) and records "condition first observed";
  it does **not** reconstruct a fill at the stop price.
- Append-only: prior events are never rewritten; a closed position is never reopened.
- On provider failure it marks `DISCONNECTED`/`STALE`, optionally falls back to the delayed floor
  **labelled `DELAYED_FALLBACK`**, preserves real-time history, does not mix timestamps, and on
  recovery appends a `RECOVERED` event without backfilling the gap.
- Unknown provider name raises — it never silently substitutes a different provider.
- The Phase 1 engine is byte-identical to what is deployed (a test pins its sha256).

## Descriptive per-trade analytics

On each simulated close the worker emits a `TRADE_ANALYTICS` event — **descriptive only**, no
scores / ranks / expected return / probabilities / recommendations / predictive models:

entry & exit timestamps, holding time, observed trading days held, MFE %, MAE %, peak profit %,
peak drawdown %, final return %, highest/lowest option value, highest/lowest underlying (raw + %
move), exit reason, the **initial-stop-only counterfactual return** on the same observed path and
whether the **trailing stop improved the result vs the initial stop**, whether trailing was active,
and whether the contract had **left the research filter before it closed** (reported from the
discovery engine's status, never inferred from price).

## Run the tests (no network, no credentials)

```bash
python3 tracker-service/test_tracker.py
```

## Try one cycle locally

```bash
python3 tracker-service/worker.py \
    --watchlist research-scanner/data/research_watchlist.json \
    --store tracker-service/tracker-data --provider tradier --once
```

---

## Going live — the exact blocker

Everything below is what a human must do; nothing here can be done unattended from this repo.

1. **Recommended first provider:** **Tradier** (reference adapter `providers/tradier.py`) if a
   funded Tradier brokerage account is acceptable — real-time options data comes with it, no
   separate data fee. Set it purely by config; the engine does not change. Alternatives are
   already pluggable by name (Polygon/Massive, Alpaca, IBKR, Schwab) once their adapter is filled in.
2. **Exact secret required:** an env var / platform secret named the adapter's `secret_env` —
   for Tradier that is **`TRADIER_TOKEN`**, a brokerage/market-data token with a **real-time
   option-quote** entitlement (a sandbox/delayed token is labelled `DELAYED`, not real-time). The
   code reads this variable and nothing else; the token is never hard-coded, logged, or written out.
3. **Create the account / key:** create a Tradier account → developer.tradier.com → API Access →
   generate an access token → confirm the market-data entitlement includes real-time options.
4. **Where the secret goes:** on the **persistent host** (step 5), as `TRADIER_TOKEN`. **Not** in
   GitHub Actions — Actions must not run the persistent loop.
5. **Hosting:** deploy `tracker-service/` to a long-lived host (Railway / Fly.io / Render / a VPS
   with systemd) and start:
   `python3 tracker-service/worker.py --watchlist <watchlist.json> --store <persistent dir>`
   with `TRACKER_PROVIDER=tradier` (or `--provider tradier`). Point `--store` at a checked-out
   git repo / Pages content dir and commit the append-only event files on a schedule to publish.
6. **What happens automatically afterward:** the worker polls only the open paper positions,
   records real-time observations, updates high-water mark + trailing stop, writes a simulated
   exit at the observed bid the first time a stop is observed, and emits the descriptive analytics
   record. The Phase 1 dashboard flips active-position provider from `NOT_CONFIGURED` to
   `tradier · LIVE` once the worker's event store is wired into the published data.

Until steps 1–5 are done by a human, this stays a scaffold and Phase 1 delayed tracking remains
the source of truth — honestly labelled as delayed.

## Railway deployment (files included)

- **`Dockerfile`** (repo root) — bundles the frozen engine + `tracker-service/`, runs
  `worker.py --serve`, reads the LIVE watchlist from the raw GitHub URL, exposes `:8080`.
- **`railway.json`** (repo root) — `DOCKERFILE` build, `healthcheckPath: /health`,
  `restartPolicyType: ON_FAILURE`, one replica.
- **`.env.example`** — copy into Railway service Variables. **No real secret is committed.**

**Persistent volume:** attach a Railway volume mounted at **`/data`** (matches `TRACKER_STATE_DIR`).
It holds the append-only events (`/data/events/`), `worker_state.json`, `heartbeat.json`,
`provider_health.jsonl`, and `checkpoint.json` — never only the ephemeral container FS.

**Environment variables (set in Railway):**

| Variable | Value | Notes |
|---|---|---|
| `TRACKER_PROVIDER` | `tradier` | primary; provider-neutral, config-only |
| `TRACKER_FALLBACK_PROVIDER` | `cboe` | labelled delayed floor |
| `TRADIER_TOKEN` | *(your token)* | **paste only in Railway**; empty ⇒ dormant NOT_CONFIGURED |
| `TRADIER_ENV` | `production` | production = real-time; sandbox = delayed |
| `TRACKER_STATE_DIR` | `/data` | the mounted volume |
| `PORT` | *(auto)* | Railway injects it; the health server binds it |

**Disabled by default:** with `TRADIER_TOKEN` unset the worker boots into a labelled
`NOT_CONFIGURED` dormant state — health endpoint returns 200, but it requests no live quotes,
fabricates nothing, marks nothing live, and evaluates no real-time exits. Phase 1 delayed tracking
is untouched. Endpoints: `GET /health` (liveness), `GET /status` (full published state).
Graceful shutdown on SIGTERM writes a final checkpoint before exit.

## Do NOT claim Phase 2 operational until

token installed · Railway running · heartbeat healthy · a real Tradier options quote succeeds ·
dashboard shows REALTIME · one open paper position receives a real-time observation · the fallback
test succeeds · an end-to-end simulated stop test succeeds.
