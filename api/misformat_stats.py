"""In-memory stats counters for the misformat_guard plugin.

Tracks per-agent and global counters. When stats_enabled is false in the
plugin config, all functions are zero-cost no-ops.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent.parent
STATS_PATH = PLUGIN_DIR / "stats" / "stats.json"
_LOCK = threading.Lock()

# Volatile counters (reset on restart). Persistent counts (e.g. total
# rep_lifetime) are flushed to disk occasionally.
_volatile: dict[str, int] = {
    "misformats_total": 0,        # total misformat warnings since start
    "repairs_total": 0,           # responses the hardened parser salvaged
    "aborts_total": 0,            # loop aborts triggered by the escape valve
    "repair_failures_total": 0,   # hardened parser also failed
    # v0.2.0+ cascade counters
    "cascade_attempts_total": 0, # every cascade attempt (success or fail)
    "cascade_calls_total": 0, # utility-model cascade invocations
    "cascade_repairs_total": 0, # cascade calls that produced a valid repair
    "cascade_failures_total": 0, # cascade calls that produced no valid repair
}


def _print(msg: str) -> None:
    sys.stderr.write(f"[misformat_guard:stats] {msg}\n")
    sys.stderr.flush()


def _enabled(agent: Any | None) -> bool:
    try:
        from usr.plugins.misformat_guard.api import misformat_config
        return bool(misformat_config.get_config(agent).get("stats_enabled", True))
    except Exception:  # noqa: BLE001
        return False


def record_misformat(agent: Any | None) -> None:
    if not _enabled(agent):
        return
    with _LOCK:
        _volatile["misformats_total"] += 1


def record_repair(agent: Any | None) -> None:
    if not _enabled(agent):
        return
    with _LOCK:
        _volatile["repairs_total"] += 1


def record_repair_failure(agent: Any | None) -> None:
    if not _enabled(agent):
        return
    with _LOCK:
        _volatile["repair_failures_total"] += 1


def record_abort(agent: Any | None) -> None:
    if not _enabled(agent):
        return
    with _LOCK:
        _volatile["aborts_total"] += 1




def record_cascade_attempt(agent: Any | None) -> None:
    """Count a cascade attempt (success or failure). Use at the start of
    every cascade call to track how often the cascade is even being
    considered, separate from whether it succeeded."""
    if not _enabled(agent):
        return
    with _LOCK:
        _volatile["cascade_attempts_total"] += 1


def record_cascade_repair(agent: Any | None) -> None:
    if not _enabled(agent):
        return
    with _LOCK:
        _volatile["cascade_calls_total"] += 1
        _volatile["cascade_repairs_total"] += 1


def record_cascade_failure(agent: Any | None) -> None:
    if not _enabled(agent):
        return
    with _LOCK:
        _volatile["cascade_calls_total"] += 1
        _volatile["cascade_failures_total"] += 1

def snapshot(agent: Any | None) -> dict[str, int]:
    with _LOCK:
        return dict(_volatile)


def reset() -> None:
    with _LOCK:
        for k in _volatile:
            _volatile[k] = 0
