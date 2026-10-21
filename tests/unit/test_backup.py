"""Unit tests for the pure helpers in recovery_operator.backup."""

from datetime import UTC, datetime

from recovery_operator.backup import (
    current_slot,
    is_due,
    is_pending,
    pvc_names,
    snapshot_name,
    snapshots_to_prune,
)


def utc(h, m, s=0):
    return datetime(2026, 1, 1, h, m, s, tzinfo=UTC)


def test_current_slot_on_boundary():
    slot = current_slot("*/2 * * * *", utc(14, 30))
    assert slot == utc(14, 30)
    assert slot.tzinfo == UTC


def test_current_slot_inside_slot():
    assert current_slot("*/2 * * * *", utc(14, 30, 20)) == utc(14, 30)
    assert current_slot("*/2 * * * *", utc(14, 31, 59)) == utc(14, 30)


def test_is_due():
    slot = utc(14, 30)
    assert is_due(None, slot) is True
    assert is_due(slot.isoformat(), slot) is False
    assert is_due(utc(14, 28).isoformat(), slot) is True


def sts(replicas=None):
    spec = {"volumeClaimTemplates": [{"metadata": {"name": "data"}}]}
    if replicas is not None:
        spec["replicas"] = replicas
    return {"metadata": {"name": "postgres"}, "spec": spec}


def test_pvc_names():
    assert pvc_names(sts(2)) == ["data-postgres-0", "data-postgres-1"]
    assert pvc_names(sts()) == ["data-postgres-0"]


def test_snapshot_name():
    assert snapshot_name("postgres", "data-postgres-0", utc(14, 30)) == (
        "postgres-data-postgres-0-202601011430"
    )


def snap(name, ts, status=None):
    s = {"metadata": {"name": name, "creationTimestamp": ts}}
    if status is not None:
        s["status"] = status
    return s


def test_is_pending():
    assert is_pending(snap("a", "t0")) is True
    assert is_pending(snap("b", "t0", {"readyToUse": True})) is False
    assert is_pending(snap("c", "t0", {"error": {"message": "boom"}})) is False


def test_snapshots_to_prune():
    snaps = [
        snap("d", "2026-01-01T14:36:00Z"),
        snap("a", "2026-01-01T14:30:00Z"),
        snap("c", "2026-01-01T14:34:00Z"),
        snap("b", "2026-01-01T14:32:00Z"),
    ]
    assert snapshots_to_prune(snaps, 2) == ["a", "b"]
    assert snapshots_to_prune(snaps, 4) == []


def test_snapshots_to_prune_tie_broken_by_name():
    snaps = [snap("z", "2026-01-01T14:30:00Z"), snap("a", "2026-01-01T14:30:00Z")]
    assert snapshots_to_prune(snaps, 1) == ["a"]
