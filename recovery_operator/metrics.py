"""Prometheus metrics that report recovery times and backup state."""

import logging

from prometheus_client import Counter, Gauge, Histogram, start_http_server

log = logging.getLogger(__name__)

ACTIONS = Counter(
    "recovery_actions_total",
    "Recovery actions by action and result.",
    ["namespace", "policy", "action", "result"],
)
BACKUPS = Counter(
    "backups_total",
    "Backup attempts by result.",
    ["namespace", "policy", "result"],
)
PRUNED = Counter(
    "snapshots_pruned_total",
    "Snapshot prune attempts by result.",
    ["namespace", "policy", "result"],
)
BACKUP_LAST_SUCCESS = Gauge(
    "backup_last_success_timestamp_seconds",
    "Creation time of the newest ready snapshot per PVC.",
    ["namespace", "policy", "pvc"],
)
ACTIVE_RECOVERIES = Gauge(
    "active_recoveries",
    "Pod recoveries and restores in flight.",
    ["namespace", "policy"],
)
FENCED_NODES = Gauge(
    "fenced_nodes",
    "Nodes this policy currently fences.",
    ["namespace", "policy"],
)
DURATIONS = Histogram(
    "recovery_duration_seconds",
    "Wall-clock time of completed recovery actions.",
    ["namespace", "policy", "action"],
    buckets=(1, 2, 5, 10, 15, 30, 60, 90, 120, 180, 300, 600, 1200),
)

_started = False


def start(port: int, addr: str = "0.0.0.0") -> bool:
    """Start the /metrics HTTP server once. Never raises."""
    global _started
    if _started:
        return True
    try:
        start_http_server(port, addr)
    except OSError as e:
        log.warning("metrics server on %s:%d failed: %s", addr, port, e)
        return False
    _started = True
    return True
