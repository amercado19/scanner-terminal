/*
 * Phase 2 dashboard panel — renders the real-time tracker's published worker_state.json.
 *
 * NOT wired into the live Phase 1 dashboard yet (index.html is frozen). This is the ready-to-merge
 * integration: at activation, host worker_state.json somewhere the dashboard can fetch (the
 * tracker's git-backed --store, or a small static endpoint) and call renderTrackerPanel(url, elId).
 *
 * Honesty gates enforced here:
 *   - LIVE badge shows ONLY when badge === "REALTIME" (which the worker sets only after a real
 *     real-time quote has actually been received — ever_received_realtime).
 *   - DELAYED / FALLBACK / NOT_CONFIGURED / STALE / DISCONNECTED each render distinctly; a stale
 *     or disconnected feed is never styled as normal.
 */
(function (global) {
  function badgeClass(badge, workerStatus) {
    if (workerStatus === "NOT_CONFIGURED") return "feed-UNKNOWN";
    if (badge === "REALTIME") return "feed-LIVE";
    if (badge === "FALLBACK") return "feed-FALLBACK_TO_CBOE";
    if (badge === "DELAYED") return "feed-DELAYED";
    return "feed-STALE";
  }

  function fmt(t) { return t ? String(t).slice(0, 19).replace("T", " ") : "—"; }

  function render(state, el) {
    if (!state) {
      el.innerHTML = '<div class="brandsub">No tracker state published yet.</div>';
      return;
    }
    var s = state;
    // LIVE is gated: never show it unless the worker itself reports REALTIME.
    var showLive = s.badge === "REALTIME" && s.ever_received_realtime === true;
    var badge = showLive ? "REAL-TIME" : (s.worker_status === "NOT_CONFIGURED"
      ? "NOT CONFIGURED" : (s.badge || "DELAYED"));
    var rows = [
      ["Primary provider", s.primary_provider || "—"],
      ["Provider mode", s.provider_mode || "—"],
      ["Worker status", s.worker_status || "—"],
      ["Provider quote ts", fmt(s.last_realtime_quote_ts || s.last_delayed_quote_ts)],
      ["Ingestion / published ts", fmt(s.published_ts)],
      ["Heartbeat", fmt(s.heartbeat_ts)],
      ["Last real-time quote", fmt(s.last_realtime_quote_ts)],
      ["Last delayed quote", fmt(s.last_delayed_quote_ts)],
      ["Active positions", s.active_positions == null ? "—" : s.active_positions],
      ["Receiving real-time", s.receiving_realtime == null ? 0 : s.receiving_realtime],
      ["On delayed fallback", s.on_delayed_fallback == null ? 0 : s.on_delayed_fallback],
      ["Stale", s.stale || 0],
      ["Disconnected", s.disconnected || 0]
    ];
    var bad = (s.stale || 0) > 0 || (s.disconnected || 0) > 0 || s.worker_status === "DISCONNECTED";
    el.innerHTML =
      '<div class="feedbox' + (bad ? " bad" : (showLive ? "" : " delayed")) + '">' +
      '<div class="feedhdr">Real-Time Tracker ' +
      '<span class="feedbadge ' + badgeClass(s.badge, s.worker_status) + '">◇ ' + badge + "</span>" +
      (bad ? '<span style="color:var(--fail)">⚠ feed not fresh</span>' : "") + "</div>" +
      rows.map(function (r) { return "<span>" + r[0] + " <b>" + r[1] + "</b></span>"; }).join("") +
      "</div>";
  }

  function renderTrackerPanel(stateUrl, elId) {
    var el = document.getElementById(elId);
    if (!el) return;
    fetch(stateUrl + "?_=" + Date.now())
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (state) { render(state, el); })
      .catch(function () { render(null, el); });
  }

  global.renderTrackerPanel = renderTrackerPanel;
  global.__trackerRender = render; // exposed for tests
})(typeof window !== "undefined" ? window : globalThis);
