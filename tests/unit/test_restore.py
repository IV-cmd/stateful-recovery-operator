"""Unit tests for the resumable restore planner in recovery_operator.recovery."""

from datetime import UTC, datetime, timedelta

from recovery_operator.recovery import (
    RestoreObservation,
    RestoreOp,
    RestoreState,
    restore_from_status,
    restore_step,
    restore_to_status,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
TIMEOUT = 180
READY_TIMEOUT = 300


def obs(**kw):
    return RestoreObservation(**kw)


def step(state, observation, now=NOW, timeout=TIMEOUT, ready=READY_TIMEOUT):
    return restore_step(state, observation, now, timeout, ready)


def test_selected_picks_snapshot():
    s = RestoreState(state="Selected", pvc="data-postgres-0", pod="postgres-0")
    s, d = step(s, obs(selected_snapshot="snap-1", selected_size="1Gi", sts_replicas=3))
    assert s.state == "Validating" and s.snapshot == "snap-1" and s.size == "1Gi"
    assert s.original_replicas == 3
    assert d.op is RestoreOp.CHECK_PVC_CREATE


def test_selected_no_snapshot_fails_without_touching():
    s = RestoreState(state="Selected", pvc="data-postgres-0")
    s, d = step(s, obs(selected_snapshot=None))
    assert s.state == "RestoreFailed" and d.op is RestoreOp.FAIL


def test_validating_creates_check_when_absent():
    s = RestoreState(state="Validating", snapshot="s", step_started_at=NOW)
    s, d = step(s, obs(check_pvc_phase=None))
    assert s.state == "Validating" and d.op is RestoreOp.CHECK_PVC_CREATE


def test_validating_never_created_and_expired_fails():
    s = RestoreState(
        state="Validating",
        snapshot="s",
        step_started_at=NOW - timedelta(seconds=200),
    )
    s, d = step(s, obs(check_pvc_phase=None), NOW)
    assert s.state == "RestoreFailed" and d.op is RestoreOp.FAIL


def test_validating_waits_while_pending_then_advances():
    s = RestoreState(state="Validating", snapshot="s", step_started_at=NOW)
    s2, d = step(s, obs(check_pvc_phase="Pending"), NOW)
    assert s2.state == "Validating" and d.op is RestoreOp.WAIT
    s2, d = step(s, obs(check_pvc_phase="Bound"), NOW)
    assert s2.state == "ScalingDown" and d.op is RestoreOp.CHECK_PVC_DELETE


def test_validating_fails_when_snapshot_gone_or_unready():
    s = RestoreState(state="Validating", snapshot="s", step_started_at=NOW)
    s2, d = step(s, obs(snapshot_exists=False, check_pvc_phase="Pending"), NOW)
    assert s2.state == "RestoreFailed" and d.op is RestoreOp.FAIL
    s2, d = step(
        s, obs(snapshot_ready=False, check_pvc_phase="Pending"), NOW
    )
    assert s2.state == "RestoreFailed"


def test_validating_timeout_fails():
    s = RestoreState(
        state="Validating",
        snapshot="s",
        step_started_at=NOW - timedelta(seconds=200),
    )
    s, d = step(s, obs(check_pvc_phase="Pending"), NOW)
    assert s.state == "RestoreFailed" and d.op is RestoreOp.FAIL


def test_scaling_down_waits_for_pods():
    s = RestoreState(state="ScalingDown", step_started_at=NOW)
    s2, d = step(s, obs(target_pods=1), NOW)
    assert s2.state == "ScalingDown" and d.op is RestoreOp.SCALE_DOWN
    s2, d = step(s, obs(target_pods=0), NOW)
    assert s2.state == "RetainingOldPv" and d.op is RestoreOp.RETAIN_PV


def test_scaling_down_resume_keeps_earlier_replicas():
    # captured on the Selected transition; a crash mid-scale-down cannot turn it into 0
    s = RestoreState(
        state="ScalingDown", original_replicas=3, step_started_at=NOW
    )
    s2, _ = step(s, obs(target_pods=1, sts_replicas=0), NOW)
    assert s2.original_replicas == 3


def test_retaining_old_pv_resumes():
    s = RestoreState(state="RetainingOldPv", step_started_at=NOW)
    s2, d = step(s, obs(old_pv_retain=False), NOW)
    assert s2.state == "RetainingOldPv" and d.op is RestoreOp.RETAIN_PV
    s2, d = step(s, obs(old_pv_retain=True), NOW)
    assert s2.state == "DeletingOldPvc" and d.op is RestoreOp.PVC_DELETE


def test_deleting_old_pvc_advances_when_already_gone():
    # resume case: PVC already deleted but state still says DeletingOldPvc
    s = RestoreState(state="DeletingOldPvc", step_started_at=NOW)
    s, d = step(s, obs(pvc_exists=False), NOW)
    assert s.state == "CreatingPvc" and d.op is RestoreOp.PVC_CREATE
    s = RestoreState(state="DeletingOldPvc", step_started_at=NOW)
    s, d = step(s, obs(pvc_exists=True), NOW)
    assert s.state == "DeletingOldPvc" and d.op is RestoreOp.PVC_DELETE


def test_creating_pvc_does_not_recreate_existing():
    # resume case: the PVC exists but the state still says CreatingPvc
    s = RestoreState(state="CreatingPvc", step_started_at=NOW)
    s, d = step(s, obs(pvc_exists=True, pvc_phase="Pending"), NOW)
    assert s.state == "CreatingPvc" and d.op is RestoreOp.WAIT
    s, d = step(s, obs(pvc_exists=True, pvc_phase="Bound"), NOW)
    assert s.state == "ScalingUp" and d.op is RestoreOp.SCALE_UP


def test_creating_pvc_timeout_fails():
    s = RestoreState(
        state="CreatingPvc", step_started_at=NOW - timedelta(seconds=200)
    )
    s, d = step(s, obs(pvc_exists=True, pvc_phase="Pending"), NOW)
    assert s.state == "RestoreFailed" and d.op is RestoreOp.FAIL


def test_scaling_up_waits_for_pod():
    s = RestoreState(state="ScalingUp", step_started_at=NOW)
    s2, d = step(s, obs(pod_ready=False), NOW)
    assert s2.state == "ScalingUp" and d.op is RestoreOp.SCALE_UP
    s2, d = step(s, obs(pod_ready=True), NOW)
    assert s2.state == "Restored" and d.op is RestoreOp.COMPLETE


def test_scaling_up_timeout_fails():
    s = RestoreState(
        state="ScalingUp",
        step_started_at=NOW - timedelta(seconds=301),
        original_replicas=1,
    )
    s, d = step(s, obs(pod_ready=False), NOW)
    assert s.state == "RestoreFailed" and d.op is RestoreOp.FAIL
    assert "not Ready after 300s" in d.reason


def test_restored_clears_next_tick():
    s = RestoreState(state="Restored", step_started_at=NOW)
    s, d = step(s, obs(), NOW)
    assert s.state == "Done" and d.op is RestoreOp.CLEAR


def test_restore_status_round_trip():
    s = RestoreState(
        state="CreatingPvc",
        pvc="data-postgres-0",
        pod="postgres-0",
        snapshot="snap-1",
        size="1Gi",
        original_replicas=1,
        old_pv="pv-1",
        started_at=NOW,
        step_started_at=NOW,
    )
    assert restore_from_status(restore_to_status(s)) == s
    assert restore_from_status(None) == RestoreState()
