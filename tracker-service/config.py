"""
Configuration for the tracker — this is the ONLY place a provider is chosen.

The worker takes a provider *instance*; it never names a broker. Which adapter that instance is
comes entirely from here, resolved by name through the provider registry. Swapping brokers is a
config change (env var or one JSON field), nothing else.

Resolution precedence (first wins):
  1. an explicit CLI value (--provider / --fallback)
  2. environment: TRACKER_PROVIDER / TRACKER_FALLBACK_PROVIDER / TRACKER_ALLOW_FALLBACK
  3. a tracker.config.json file (path via --config or TRACKER_CONFIG, else ./tracker.config.json)
  4. defaults: provider="cboe" (the honest DELAYED floor), fallback="cboe", allow_fallback=True

Default is "cboe" on purpose: with nothing configured the tracker runs on the same honest, clearly
labelled DELAYED data as Phase 1 — it never silently pretends to be real-time. Point it at a
real-time adapter only by explicit config.

State/volume: the worker writes its append-only events, current state, heartbeat, provider-health
history, and recovery checkpoints under TRACKER_STATE_DIR (Railway persistent-volume mount), never
only the ephemeral container filesystem.
"""
from __future__ import annotations
import os
import json
from typing import Any, Dict, Optional

DEFAULT_PROVIDER = "cboe"
DEFAULT_FALLBACK = "cboe"
DEFAULT_STATE_DIR = os.environ.get("TRACKER_STATE_DIR", "/data")   # Railway volume mount default


def _load_file(path: Optional[str]) -> Dict[str, Any]:
    candidates = [path] if path else []
    candidates += [os.environ.get("TRACKER_CONFIG"), "tracker.config.json"]
    for c in candidates:
        if c and os.path.exists(c):
            try:
                with open(c, encoding="utf-8") as f:
                    return json.load(f) or {}
            except Exception:
                return {}
    return {}


def _as_bool(v, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load_config(cli_provider: Optional[str] = None,
                cli_fallback: Optional[str] = None,
                cli_allow_fallback: Optional[bool] = None,
                config_path: Optional[str] = None) -> Dict[str, Any]:
    """Return the resolved tracker config. The worker consumes `provider`/`fallback` names and
    hands them to the registry; it is told nothing else about the broker."""
    filecfg = _load_file(config_path)

    provider = (cli_provider
                or os.environ.get("TRACKER_PROVIDER")
                or filecfg.get("provider")
                or DEFAULT_PROVIDER)

    fallback = (cli_fallback
                or os.environ.get("TRACKER_FALLBACK_PROVIDER")
                or os.environ.get("TRACKER_FALLBACK")   # accepted alias
                or filecfg.get("fallback")
                or DEFAULT_FALLBACK)

    allow_fallback = cli_allow_fallback
    if allow_fallback is None:
        allow_fallback = _as_bool(os.environ.get("TRACKER_ALLOW_FALLBACK"),
                                  _as_bool(filecfg.get("allow_fallback"), True))

    state_dir = (os.environ.get("TRACKER_STATE_DIR")
                 or filecfg.get("state_dir")
                 or DEFAULT_STATE_DIR)

    return {
        "provider": provider.lower(),
        "provider_kwargs": filecfg.get("provider_kwargs") or {},
        "fallback": (fallback or "").lower() or None,
        "fallback_kwargs": filecfg.get("fallback_kwargs") or {},
        "allow_fallback": bool(allow_fallback),
        "state_dir": state_dir,
        "poll_interval_sec": int(os.environ.get("TRACKER_POLL_INTERVAL_SEC")
                                 or filecfg.get("poll_interval_sec") or 20),
        "health_port": int(os.environ.get("PORT") or os.environ.get("TRACKER_HEALTH_PORT")
                           or filecfg.get("health_port") or 8080),
    }
