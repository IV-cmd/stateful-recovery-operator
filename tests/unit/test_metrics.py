"""Unit tests for recovery_operator.metrics."""

import socket
from pathlib import Path

from recovery_operator import metrics


def test_start_twice_does_not_raise():
    # may already be started by an earlier test in this process; both calls pass
    assert metrics.start(0) is True
    assert metrics.start(0) is True


def test_start_on_bound_port_returns_false():
    metrics._started = False
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    try:
        assert metrics.start(port, "127.0.0.1") is False
    finally:
        sock.close()
        metrics._started = True  # keep the guard set for the rest of the suite


def test_recovery_py_has_no_prometheus_import():
    src = Path(metrics.__file__).with_name("recovery.py").read_text()
    assert "prometheus" not in src


def test_duration_buckets():
    finite = [b for b in metrics.DURATIONS._upper_bounds if b != float("inf")]
    assert finite == [1, 2, 5, 10, 15, 30, 60, 90, 120, 180, 300, 600, 1200]


def test_init_policy_creates_zero_samples():
    metrics.init_policy("ns1", "pol1")
    samples = {
        (s.labels["action"], s.labels["result"])
        for s in metrics.ACTIONS.collect()[0].samples
        if s.labels["namespace"] == "ns1" and s.labels["policy"] == "pol1"
    }
    assert len(samples) == 18
    backup_results = {
        s.labels["result"]
        for s in metrics.BACKUPS.collect()[0].samples
        if s.labels["namespace"] == "ns1" and s.labels["policy"] == "pol1"
    }
    assert backup_results == {"created", "skipped_pending", "skipped_unhealthy", "error"}
    durations = {
        s.labels["action"]
        for s in metrics.DURATIONS.collect()[0].samples
        if s.labels["namespace"] == "ns1"
        and s.labels["policy"] == "pol1"
        and s.name.endswith("_count")
    }
    assert durations == {"restart", "fence", "restore"}
