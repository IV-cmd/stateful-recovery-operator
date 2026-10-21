"""Unit tests for the pure recovery logic in recovery_operator.recovery."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from recovery_operator.recovery import (
    Action,
    PodMemory,
    Policy,
    decide,
    from_status,
    observe,
    pod_view,
    to_status,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
POLICY = Policy(max_restarts=3, unready_seconds=120, max_concurrent=1)


def crashloop_pod(name="postgres-0", uid="u1"):
    return {
        "metadata": {"name": name, "uid": uid},
        "status": {
            "conditions": [{"type": "Ready", "status": "False"}],
            "containerStatuses": [
                {"state": {"waiting": {"reason": "CrashLoopBackOff"}}}
            ],
        },
    }


def ready_pod(name="postgres-0", uid="u1"):
    return {
        "metadata": {"name": name, "uid": uid},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }


def test_pod_view_crashloop():
    v = pod_view(crashloop_pod())
    assert v.crash_looping is True and v.ready is False and v.terminating is False


def test_pod_view_ready():
    v = pod_view(ready_pod())
    assert v.ready is True and v.crash_looping is False


def test_pod_view_no_status():
    v = pod_view({"metadata": {"name": "p", "uid": "u"}})
    assert v.ready is False and v.crash_looping is False


def test_pod_view_terminating():
    pod = ready_pod()
    pod["metadata"]["deletionTimestamp"] = "2026-01-01T12:00:00Z"
    assert pod_view(pod).terminating is True


def view(pod_dict):
    return pod_view(pod_dict)


def test_observe_new_uid_resets_unready():
    mem = PodMemory(uid="old", unready_since=NOW)
    new, _ = observe(view(crashloop_pod(uid="new")), mem, NOW)
    assert new.unready_since == NOW  # clock restarts: pod is unready right now
    assert new.uid == "new"


def test_observe_starts_clock_on_first_unready():
    mem, _ = observe(view(crashloop_pod()), PodMemory(), NOW)
    assert mem.unready_since == NOW


def test_observe_ready_after_delete_recovers():
    mem = PodMemory(uid="u1", restarts=1, deleted_uid="u1")
    new, recovered = observe(view(ready_pod(uid="u2")), mem, NOW)
    assert recovered is True
    assert new.restarts == 0 and new.deleted_uid is None and new.escalated is False


def test_decide_healthy_skips():
    mem, _ = observe(view(ready_pod()), PodMemory(), NOW)
    assert decide(view(ready_pod()), mem, POLICY, 0, NOW).action is Action.SKIP


def test_decide_terminating_or_deleted_skips():
    pod = crashloop_pod()
    pod["metadata"]["deletionTimestamp"] = "2026-01-01T12:00:00Z"
    mem = PodMemory(uid="u1")
    assert decide(view(pod), mem, POLICY, 0, NOW).action is Action.SKIP
    mem = PodMemory(uid="u1", deleted_uid="u1")
    assert decide(view(crashloop_pod()), mem, POLICY, 0, NOW).action is Action.SKIP


def test_decide_crashloop_restarts():
    mem, _ = observe(view(crashloop_pod()), PodMemory(), NOW)
    d = decide(view(crashloop_pod()), mem, POLICY, 0, NOW)
    assert d.action is Action.RESTART and d.reason == "CrashLoopBackOff"


def test_decide_unready_waits_then_restarts():
    pod = view({"metadata": {"name": "p", "uid": "u"}})
    mem = PodMemory(uid="u", unready_since=NOW - timedelta(seconds=10))
    assert decide(pod, mem, POLICY, 0, NOW).action is Action.WAIT
    mem = PodMemory(uid="u", unready_since=NOW - timedelta(seconds=120))
    assert decide(pod, mem, POLICY, 0, NOW).action is Action.RESTART


def test_decide_escalates_over_cap():
    mem = PodMemory(uid="u", restarts=3)
    d = decide(view(crashloop_pod()), mem, POLICY, 1, NOW)
    assert d.action is Action.ESCALATE


def test_decide_cap_waits():
    mem = PodMemory(uid="u", restarts=1)
    d = decide(view(crashloop_pod()), mem, POLICY, 1, NOW)
    assert d.action is Action.WAIT and "concurrency" in d.reason


def test_decide_zero_max_restarts_escalates():
    mem = PodMemory(uid="u")
    d = decide(view(crashloop_pod()), mem, Policy(0, 120, 1), 0, NOW)
    assert d.action is Action.ESCALATE


def crashloop_view(name="postgres-0", uid="u1", node_ready=True):
    return pod_view(crashloop_pod(name, uid), node_ready=node_ready)


def test_observe_failing_since_starts_and_persists():
    mem, _ = observe(crashloop_view(), PodMemory(), NOW)
    assert mem.failing_since == NOW
    # persists across replacement UIDs
    mem, _ = observe(crashloop_view(uid="u2"), mem, NOW + timedelta(seconds=30))
    assert mem.failing_since == NOW
    # cleared on Ready
    mem, _ = observe(pod_view(ready_pod(uid="u3")), mem, NOW + timedelta(seconds=60))
    assert mem.failing_since is None


def test_observe_node_outage_counts_nothing():
    pod = crashloop_view(node_ready=False)
    mem, _ = observe(pod, PodMemory(), NOW)
    assert mem.unready_since is None and mem.failing_since is None
    # a running unready clock is cleared while the node is down
    mem, _ = observe(pod, PodMemory(uid="u1", unready_since=NOW), NOW)
    assert mem.unready_since is None


def test_decide_node_outage_never_escalates():
    mem = PodMemory(uid="u1", restarts=3, unready_since=NOW - timedelta(hours=1))
    d = decide(crashloop_view(node_ready=False), mem, POLICY, 0, NOW)
    assert d.action is Action.WAIT and "node unhealthy" in d.reason


def test_decide_restore_enabled_restores():
    mem = PodMemory(uid="u1", restarts=2)
    d = decide(crashloop_view(), mem, Policy(2, 120, 1, restore_enabled=True), 0, NOW)
    assert d.action is Action.RESTORE


def test_decide_restore_disabled_escalates():
    mem = PodMemory(uid="u1", restarts=2)
    d = decide(crashloop_view(), mem, Policy(2, 120, 1), 0, NOW)
    assert d.action is Action.ESCALATE


def test_decide_restored_pod_escalates_not_restores():
    mem = PodMemory(uid="u1", restarts=2, restored=True)
    d = decide(crashloop_view(), mem, Policy(2, 120, 1, restore_enabled=True), 0, NOW)
    assert d.action is Action.ESCALATE


def test_decide_new_failure_after_ready_can_restore_again():
    policy = Policy(1, 120, 1, restore_enabled=True)
    mem, _ = observe(crashloop_view(), PodMemory(restarts=1, restored=True), NOW)
    mem, _ = observe(pod_view(ready_pod(uid="u2")), mem, NOW)
    assert mem.restored is False and mem.failing_since is None
    mem, _ = observe(crashloop_view(uid="u3"), mem, NOW)
    mem = replace(mem, restarts=1)
    d = decide(crashloop_view(uid="u3"), mem, policy, 0, NOW)
    assert d.action is Action.RESTORE


def test_status_round_trip():
    mem = PodMemory(
        uid="u",
        unready_since=NOW,
        restarts=2,
        deleted_uid="d",
        dry_run_uid="dr",
        escalated=True,
    )
    assert from_status(to_status(mem)) == mem
    assert from_status(None) == PodMemory()
    assert "unreadySince" not in to_status(PodMemory())
