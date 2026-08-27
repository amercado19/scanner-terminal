# Dashboard integration (Phase 2) — ready-to-merge, NOT yet applied

Phase 1's `index.html` is **frozen and deployed**. This folder is the ready-to-merge integration
that surfaces the real-time tracker on the dashboard **at activation** — it is intentionally not
wired into the live dashboard yet, so nothing shows LIVE until a real Tradier quote has been received.

## What the worker publishes

When running, the worker writes `worker_state.json` to its persistent `--store`/volume every cycle
(`TRACKER_STATE_DIR`, e.g. `/data/worker_state.json`). It contains exactly the fields the dashboard
must show: primary provider, provider mode, badge (REALTIME/DELAYED/FALLBACK/NOT_CONFIGURED),
provider quote timestamp, ingestion/published timestamp, observed latency inputs, heartbeat, last
successful real-time quote, last successful delayed quote, active-position count, real-time-tracked
count, fallback count, stale count, disconnected count, and worker status.

## The LIVE gate

`worker_state.json.badge === "REALTIME"` only after the worker has actually received a real
real-time quote (`ever_received_realtime === true`). `tracker_panel.js` shows the LIVE / REAL-TIME
badge **only** in that case; until then it renders NOT CONFIGURED or DELAYED. **Do not** display
LIVE from any other signal.

## Applying it (only when activating Phase 2)

1. Publish `worker_state.json` where the dashboard's origin can fetch it (commit it from the
   tracker's git-backed `--store`, or serve it statically).
2. Add to `index.html`, inside a new "Real-Time Tracker" block in the Provider / Status pane:
   ```html
   <div id="tracker-panel"></div>
   <script src="tracker-service/dashboard/tracker_panel.js"></script>
   <script>renderTrackerPanel("<worker_state.json URL>", "tracker-panel");</script>
   ```
   The panel reuses the existing feed CSS classes already in `index.html`
   (`.feedbox`, `.feedbadge`, `.feed-LIVE/DELAYED/STALE`), so no new styles are needed.
3. Re-run `test_research_scanner.py`'s dashboard-render test (it must still pass) and re-verify the
   Data-Feed System Status section still renders.

Because this changes the frozen Phase 1 `index.html`, it is a **separate, explicit step taken at
activation** — not part of committing the inactive scaffold.
