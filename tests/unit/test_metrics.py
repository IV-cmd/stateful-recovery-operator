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
