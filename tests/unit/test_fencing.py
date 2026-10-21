"""Unit tests for node fencing decisions in recovery_operator.recovery."""

from datetime import UTC, datetime, timedelta

from recovery_operator.recovery import (
    FenceMemory,
    FencePolicy,
    NodeAction,
    NodeSituation,
    NodeView,
    decide_node,
    fence_from_status,
    fence_to_status,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
POLICY = FencePolicy(enabled=True, dry_run=False, not_ready_seconds=60, verify_seconds=180)
T0 = NOW - timedelta(seconds=120)


def node(ready=True, uid="u1", transition=T0, unreachable=False, oos=False, fenced=False, cp=False):
    return NodeView(
        name="worker3",
        uid=uid,
        ready=ready,
        ready_since_transition=transition,
        unreachable_taint=unreachable,
        oos_taint=oos,
        fenced_by_us=fenced,
        control_plane=cp,
    )


def sit(pods=1, gone=False, attachments=0, others=0):
    return NodeSituation(
        target_pods_on_node=pods,
        target_pods_gone=gone,
        attachments_left=attachments,
        other_unhealthy_nodes=others,
    )


def test_control_plane_skips():
    _mem, d = decide_node(node(ready=False, unreachable=True, cp=True), FenceMemory(), sit(), POLICY, NOW)
    assert d.action is NodeAction.SKIP


def test_healthy_no_memory_skips():
    _, d = decide_node(node(ready=True), FenceMemory(), sit(), POLICY, NOW)
    assert d.action is NodeAction.SKIP and d.reason == "healthy node"


def test_no_target_pods_skips():
    _, d = decide_node(node(ready=False, unreachable=True), FenceMemory(), sit(pods=0), POLICY, NOW)
    assert d.action is NodeAction.SKIP


def test_disabled_skips():
    p = FencePolicy(enabled=False, dry_run=False, not_ready_seconds=60, verify_seconds=180)
    _, d = decide_node(node(ready=False, unreachable=True), FenceMemory(), sit(), p, NOW)
    assert d.action is NodeAction.SKIP


def test_below_threshold_waits_with_detected():
    view = node(ready=False, transition=NOW - timedelta(seconds=10), unreachable=True)
    mem, d = decide_node(view, FenceMemory(), sit(), POLICY, NOW)
    assert d.action is NodeAction.WAIT and mem.state == "Detected" and mem.detected_at == NOW


def test_no_unreachable_taint_waits():
    view = node(ready=False, transition=T0, unreachable=False)
    mem, d = decide_node(view, FenceMemory(), sit(), POLICY, NOW)
    assert d.action is NodeAction.WAIT and mem.state == "Detected"


def test_other_unhealthy_refuses_once():
    view = node(ready=False, unreachable=True)
    mem, d = decide_node(view, FenceMemory(), sit(others=1), POLICY, NOW)
    assert d.action is NodeAction.REFUSE and mem.refused
    _, d2 = decide_node(view, mem, sit(others=1), POLICY, NOW)
    assert d2.action is NodeAction.WAIT


def test_dry_run_once():
    p = FencePolicy(enabled=True, dry_run=True, not_ready_seconds=60, verify_seconds=180)
    view = node(ready=False, unreachable=True)
    mem, d = decide_node(view, FenceMemory(), sit(), p, NOW)
    assert d.action is NodeAction.DRY_RUN and mem.dry_run_done
    _, d2 = decide_node(view, mem, sit(), p, NOW)
    assert d2.action is NodeAction.WAIT


def test_fence():
    view = node(ready=False, unreachable=True)
    mem, d = decide_node(view, FenceMemory(), sit(), POLICY, NOW)
    assert d.action is NodeAction.FENCE and mem.state == "Fencing" and mem.fenced_at == NOW


def fenced_mem(state="Fenced"):
    return FenceMemory(state=state, node_uid="u1", fenced_at=NOW - timedelta(seconds=30))


def test_verify_ok_once():
    view = node(ready=False, unreachable=True, oos=True, fenced=True)
    mem, d = decide_node(view, fenced_mem(), sit(gone=True, attachments=0), POLICY, NOW)
    assert d.action is NodeAction.VERIFY_OK and mem.state == "Verified"
    _, d2 = decide_node(view, mem, sit(gone=True, attachments=0), POLICY, NOW)
    assert d2.action is NodeAction.WAIT


def test_wait_until_pods_gone_and_attachments_clear():
    view = node(ready=False, unreachable=True, oos=True, fenced=True)
    _, d = decide_node(view, fenced_mem(), sit(gone=True, attachments=1), POLICY, NOW)
    assert d.action is NodeAction.WAIT
    _, d = decide_node(view, fenced_mem(), sit(gone=False, attachments=0), POLICY, NOW)
    assert d.action is NodeAction.WAIT


def test_verify_timeout_warns_once():
    p = FencePolicy(enabled=True, dry_run=False, not_ready_seconds=60, verify_seconds=10)
    view = node(ready=False, unreachable=True, oos=True, fenced=True)
    mem, d = decide_node(view, fenced_mem(), sit(gone=False), p, NOW)
    assert d.action is NodeAction.VERIFY_TIMEOUT and mem.warned_unverified
    _, d2 = decide_node(view, mem, sit(gone=False), p, NOW)
    assert d2.action is NodeAction.WAIT


def test_resume_after_restart_fences_again():
    # state was Fencing but the status write died with the process, or the taint was removed
    view = node(ready=False, unreachable=True, oos=False, fenced=False)
    _mem, d = decide_node(view, fenced_mem("Fencing"), sit(), POLICY, NOW)
    assert d.action is NodeAction.FENCE


def test_restart_resumes_from_annotation():
    # memory gone entirely, but the node still carries our annotation
    view = node(ready=False, unreachable=True, oos=True, fenced=True)
    _mem, d = decide_node(view, FenceMemory(), sit(gone=True, attachments=0), POLICY, NOW)
    assert d.action is NodeAction.VERIFY_OK


def test_unfence_on_ready():
    view = node(ready=True, oos=True, fenced=True)
    mem, d = decide_node(view, fenced_mem("Verified"), sit(pods=0, gone=True), POLICY, NOW)
    assert d.action is NodeAction.UNFENCE and mem.state == "Recovering"


def test_recovered_clears_entry():
    view = node(ready=True, oos=False, fenced=False)
    mem = FenceMemory(state="Recovering", node_uid="u1")
    new_mem, d = decide_node(view, mem, sit(), POLICY, NOW)
    assert d.action is NodeAction.SKIP and new_mem.state is None


def test_foreign_taint_skips():
    view = node(ready=True, oos=True, fenced=False)
    _, d = decide_node(view, FenceMemory(), sit(), POLICY, NOW)
    assert d.action is NodeAction.SKIP and "someone else" in d.reason
    view2 = node(ready=False, unreachable=True, oos=True, fenced=False)
    _, d2 = decide_node(view2, FenceMemory(), sit(), POLICY, NOW)
    assert d2.action is NodeAction.SKIP


def test_uid_change_resets_memory():
    view = node(ready=True, uid="new-uid")
    mem = FenceMemory(state="Fenced", node_uid="old-uid", fenced_at=NOW)
    new_mem, _ = decide_node(view, mem, sit(), POLICY, NOW)
    assert new_mem.node_uid == "new-uid" and new_mem.state is None


def test_fence_status_round_trip():
    mem = FenceMemory(
        state="Verified",
        node_uid="u",
        detected_at=T0,
        fenced_at=NOW,
        verified_at=NOW,
        dry_run_done=True,
        warned_unverified=True,
    )
    assert fence_from_status(fence_to_status(mem)) == mem
    assert fence_from_status(None) == FenceMemory()
    assert "state" not in fence_to_status(FenceMemory())
