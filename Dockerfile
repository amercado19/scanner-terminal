# Railway image for the Phase 2 real-time active-position tracker (tracker-service/).
# Phase 1 (GitHub Actions + Pages) is unaffected by this file. Stdlib-only — no pip install needed.
#
# The image bundles the FROZEN Phase 1 engine (research_scanner.py) so the tracker reuses its stop
# policy without forking. It reads the LIVE watchlist from the raw GitHub URL at runtime, so it
# always sees the current open paper positions (not a stale build-time copy).
#
# DISABLED BY DEFAULT: with no TRADIER_TOKEN set, the worker boots into a labelled NOT_CONFIGURED
# dormant state — it does not request live quotes, fabricate observations, or mark anything live.
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRACKER_STATE_DIR=/data

# frozen Phase 1 engine (byte-identical to what is deployed) + the tracker package
COPY research-scanner/research_scanner.py /app/research-scanner/research_scanner.py
COPY tracker-service/ /app/tracker-service/

# persistent-volume mount point (Railway volume attaches here). Never rely only on the
# ephemeral container FS for events / state / heartbeat / provider-health / checkpoints.
VOLUME ["/data"]
EXPOSE 8080

# provider/fallback/token/env all come from Railway env vars; nothing broker-specific is baked in.
CMD ["python", "tracker-service/worker.py", "--serve", \
     "--watchlist", "https://raw.githubusercontent.com/amercado19/scanner-terminal/main/research-scanner/data/research_watchlist.json"]
