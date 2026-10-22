"""Failure detection and recovery flow for pod, node, and volume failures."""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum


class Action(Enum):
    RESTART = "RESTART"
    RESTORE = "RESTORE"
    ESCALATE = "ESCALATE"
    WAIT = "WAIT"
    SKIP = "SKIP"


@dataclass(frozen=True)
class PodView:
    name: str
    uid: str
    terminating: bool
    ready: bool
    crash_looping: bool
    node_ready: bool = True


@dataclass(frozen=True)
class PodMemory:
    """One entry per pod name, stored in status.recovery.pods[name]."""

    uid: str | None = None
    unready_since: datetime | None = None
    restarts: int = 0
    deleted_uid: str | None = None
    deleted_at: datetime | None = None
    dry_run_uid: str | None = None
    escalated: bool = False
    failing_since: datetime | None = None
    restored: bool = False
    restore_dry_run: bool = False


@dataclass(frozen=True)
class Policy:
    max_restarts: int
    unready_seconds: int
    max_concurrent: int
    restore_enabled: bool = False


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str


def pod_view(pod: dict, node_ready: bool = True) -> PodView:
    """Return the fields the recovery logic needs from a pod dict."""
    meta = pod.get("metadata", {})
    status = pod.get("status") or {}
    ready = any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in status.get("conditions") or []
    )
    crash_looping = any(
        (cs.get("state") or {}).get("waiting", {}).get("reason") == "CrashLoopBackOff"
        for cs in status.get("containerStatuses") or []
    )
    return PodView(
        name=meta["name"],
        uid=meta["uid"],
        terminating=meta.get("deletionTimestamp") is not None,
        ready=ready,
        crash_looping=crash_looping,
        node_ready=node_ready,
    )


def observe(pod: PodView, mem: PodMemory, now: datetime) -> tuple[PodMemory, bool]:
    """Update the memory for this pod. Return (memory, recovered)."""
    recovered = False
    if mem.uid != pod.uid:
        mem = replace(mem, unready_since=None, dry_run_uid=None)
    if pod.ready:
        if mem.deleted_uid or mem.restarts or mem.escalated or mem.restored:
            recovered = True
        mem = replace(
            mem,
            unready_since=None,
            deleted_uid=None,
            deleted_at=None,
            restarts=0,
            escalated=False,
            failing_since=None,
            restored=False,
            restore_dry_run=False,
        )
    elif pod.node_ready:
        if mem.unready_since is None:
            mem = replace(mem, unready_since=now)
        if mem.failing_since is None:
            mem = replace(mem, failing_since=now)
    else:
        # node outage: do not count unready time, fencing owns the failure
        mem = replace(mem, unready_since=None)
    return replace(mem, uid=pod.uid), recovered


def restart_duration(prev: PodMemory, recovered: bool, now: datetime) -> float | None:
    """Seconds a pod took to come back Ready after we deleted it.

    None when the recovery was not a restart: a restored pod, an escalation
    that reset without a delete, or a pod still down.
    """
    if not (recovered and prev.deleted_at and not prev.restored):
        return None
    return (now - prev.deleted_at).total_seconds()


def decide(
    pod: PodView, mem: PodMemory, policy: Policy, in_flight_others: int, now: datetime
) -> Decision:
    """Pick the recovery action for an already-observed pod."""
    if pod.terminating or pod.uid == mem.deleted_uid:
        return Decision(Action.SKIP, "already recovering")
    if pod.ready:
        return Decision(Action.SKIP, "healthy")
    if not pod.node_ready:
        return Decision(Action.WAIT, "node unhealthy, left to fencing")
    if pod.crash_looping:
        reason = "CrashLoopBackOff"
    else:
        unready_for = (now - mem.unready_since).total_seconds() if mem.unready_since else 0
        if unready_for < policy.unready_seconds:
            return Decision(Action.WAIT, f"unready for {int(unready_for)}s")
        reason = f"unready for {int(unready_for)}s"
    if mem.restarts >= policy.max_restarts:
        if policy.restore_enabled and not mem.restored:
            return Decision(Action.RESTORE, reason)
        return Decision(Action.ESCALATE, reason)
    if in_flight_others >= policy.max_concurrent:
        return Decision(Action.WAIT, "concurrency cap reached")
    return Decision(Action.RESTART, reason)


def to_status(mem: PodMemory) -> dict:
    """Serialize a PodMemory for status.recovery.pods[name]."""
    out = {
        "uid": mem.uid,
        "unreadySince": mem.unready_since.isoformat() if mem.unready_since else None,
        "restarts": mem.restarts,
        "deletedUid": mem.deleted_uid,
        "deletedAt": mem.deleted_at.isoformat() if mem.deleted_at else None,
        "dryRunUid": mem.dry_run_uid,
        "escalated": mem.escalated,
        "failingSince": mem.failing_since.isoformat() if mem.failing_since else None,
        "restored": mem.restored or None,
        "restoreDryRun": mem.restore_dry_run or None,
    }
    return {k: v for k, v in out.items() if v is not None}


def from_status(d: dict | None) -> PodMemory:
    """Load a PodMemory from status.recovery.pods[name]."""
    d = d or {}
    since = d.get("unreadySince")
    return PodMemory(
        uid=d.get("uid"),
        unready_since=datetime.fromisoformat(since) if since else None,
        restarts=d.get("restarts", 0),
        deleted_uid=d.get("deletedUid"),
        deleted_at=(
            datetime.fromisoformat(d["deletedAt"]) if d.get("deletedAt") else None
        ),
        dry_run_uid=d.get("dryRunUid"),
        escalated=d.get("escalated", False),
        failing_since=(
            datetime.fromisoformat(d["failingSince"]) if d.get("failingSince") else None
        ),
        restored=d.get("restored", False),
        restore_dry_run=d.get("restoreDryRun", False),
    )


class NodeAction(Enum):
    FENCE = "FENCE"
    DRY_RUN = "DRY_RUN"
    VERIFY_OK = "VERIFY_OK"
    VERIFY_TIMEOUT = "VERIFY_TIMEOUT"
    UNFENCE = "UNFENCE"
    WAIT = "WAIT"
    SKIP = "SKIP"
    REFUSE = "REFUSE"


@dataclass(frozen=True)
class NodeView:
    name: str
    uid: str
    ready: bool
    ready_since_transition: datetime | None
    unreachable_taint: bool
    oos_taint: bool
    fenced_by_us: bool
    control_plane: bool


@dataclass(frozen=True)
class NodeSituation:
    target_pods_on_node: int
    target_pods_gone: bool
    attachments_left: int
    other_unhealthy_nodes: int


@dataclass(frozen=True)
class FenceMemory:
    """One entry per node name, stored in status.recovery.fencing[node]."""

    state: str | None = None
    node_uid: str | None = None
    detected_at: datetime | None = None
    fenced_at: datetime | None = None
    verified_at: datetime | None = None
    dry_run_done: bool = False
    refused: bool = False
    warned_unverified: bool = False


@dataclass(frozen=True)
class FencePolicy:
    enabled: bool
    dry_run: bool
    not_ready_seconds: int
    verify_seconds: int


@dataclass(frozen=True)
class NodeDecision:
    action: NodeAction
    reason: str


def decide_node(
    view: NodeView,
    mem: FenceMemory,
    sit: NodeSituation,
    policy: FencePolicy,
    now: datetime,
) -> tuple[FenceMemory, NodeDecision]:
    """Pick the fencing action for one node and update the fence memory."""
    if mem.node_uid != view.uid:
        mem = FenceMemory(node_uid=view.uid)

    if view.control_plane:
        return mem, NodeDecision(NodeAction.SKIP, "control plane")

    if view.ready:
        if view.fenced_by_us:
            if mem.state == "Recovering":
                return mem, NodeDecision(NodeAction.WAIT, "unfence in progress")
            return replace(mem, state="Recovering"), NodeDecision(
                NodeAction.UNFENCE, "node is Ready again"
            )
        if view.oos_taint:
            return mem, NodeDecision(NodeAction.SKIP, "taint owned by someone else")
        if mem.state is not None:
            return replace(mem, state=None), NodeDecision(NodeAction.SKIP, "recovered")
        return mem, NodeDecision(NodeAction.SKIP, "healthy node")

    # node is not ready from here on
    if view.oos_taint and not view.fenced_by_us:
        return mem, NodeDecision(NodeAction.SKIP, "taint owned by someone else")

    if mem.state in ("Fencing", "Fenced", "Verified") or view.fenced_by_us:
        if not view.oos_taint or not view.fenced_by_us:
            # a restart mid-fence lost the state or the taint; the patch is idempotent
            return replace(
                mem, state="Fencing", fenced_at=mem.fenced_at or now
            ), NodeDecision(NodeAction.FENCE, "taint missing, resuming fence")
        if mem.state != "Verified":
            if sit.target_pods_gone and sit.attachments_left == 0:
                return replace(mem, state="Verified", verified_at=now), NodeDecision(
                    NodeAction.VERIFY_OK, "pods gone and attachments cleared"
                )
            if mem.fenced_at and (now - mem.fenced_at).total_seconds() > policy.verify_seconds:
                if not mem.warned_unverified:
                    return replace(mem, warned_unverified=True), NodeDecision(
                        NodeAction.VERIFY_TIMEOUT,
                        f"not verified after {policy.verify_seconds}s",
                    )
                return mem, NodeDecision(NodeAction.WAIT, "already warned")
        return mem, NodeDecision(NodeAction.WAIT, "waiting for pods and attachments to clear")

    if mem.state is None and sit.target_pods_on_node == 0:
        return mem, NodeDecision(NodeAction.SKIP, "no target pods on this node")
    if not policy.enabled:
        return mem, NodeDecision(NodeAction.SKIP, "fencing disabled")

    elapsed = (
        (now - view.ready_since_transition).total_seconds()
        if view.ready_since_transition
        else 0
    )
    if elapsed < policy.not_ready_seconds:
        return replace(mem, state="Detected", detected_at=mem.detected_at or now), NodeDecision(
            NodeAction.WAIT, f"unready for {int(elapsed)}s"
        )
    if not view.unreachable_taint:
        return replace(mem, state="Detected", detected_at=mem.detected_at or now), NodeDecision(
            NodeAction.WAIT, "no unreachable taint yet"
        )
    if sit.other_unhealthy_nodes > 0:
        if not mem.refused:
            return replace(mem, refused=True, state="Detected"), NodeDecision(
                NodeAction.REFUSE, "other unhealthy nodes, possible partition"
            )
        return mem, NodeDecision(NodeAction.WAIT, "refused earlier")
    if policy.dry_run:
        if not mem.dry_run_done:
            return replace(mem, dry_run_done=True, state="Detected"), NodeDecision(
                NodeAction.DRY_RUN, "would fence this node"
            )
        return mem, NodeDecision(NodeAction.WAIT, "dry run done")
    return replace(mem, state="Fencing", fenced_at=now), NodeDecision(
        NodeAction.FENCE, "fencing this node"
    )


def fence_to_status(mem: FenceMemory) -> dict:
    """Serialize a FenceMemory for status.recovery.fencing[node]."""
    out = {
        "state": mem.state,
        "nodeUid": mem.node_uid,
        "detectedAt": mem.detected_at.isoformat() if mem.detected_at else None,
        "fencedAt": mem.fenced_at.isoformat() if mem.fenced_at else None,
        "verifiedAt": mem.verified_at.isoformat() if mem.verified_at else None,
        "dryRunDone": mem.dry_run_done or None,
        "refused": mem.refused or None,
        "warnedUnverified": mem.warned_unverified or None,
    }
    return {k: v for k, v in out.items() if v is not None}


def fence_from_status(d: dict | None) -> FenceMemory:
    """Load a FenceMemory from status.recovery.fencing[node]."""
    d = d or {}
    return FenceMemory(
        state=d.get("state"),
        node_uid=d.get("nodeUid"),
        detected_at=datetime.fromisoformat(d["detectedAt"]) if d.get("detectedAt") else None,
        fenced_at=datetime.fromisoformat(d["fencedAt"]) if d.get("fencedAt") else None,
        verified_at=datetime.fromisoformat(d["verifiedAt"]) if d.get("verifiedAt") else None,
        dry_run_done=d.get("dryRunDone", False),
        refused=d.get("refused", False),
        warned_unverified=d.get("warnedUnverified", False),
    )


class RestoreOp(Enum):
    CHECK_PVC_CREATE = "CHECK_PVC_CREATE"
    CHECK_PVC_DELETE = "CHECK_PVC_DELETE"
    SCALE_DOWN = "SCALE_DOWN"
    RETAIN_PV = "RETAIN_PV"
    PVC_DELETE = "PVC_DELETE"
    PVC_CREATE = "PVC_CREATE"
    SCALE_UP = "SCALE_UP"
    COMPLETE = "COMPLETE"
    FAIL = "FAIL"
    CLEAR = "CLEAR"
    WAIT = "WAIT"


@dataclass(frozen=True)
class RestoreState:
    """Stored in status.recovery.restore; one restore at a time."""

    state: str | None = None
    pvc: str | None = None
    pod: str | None = None
    snapshot: str | None = None
    size: str | None = None
    original_replicas: int | None = None
    old_pv: str | None = None
    started_at: datetime | None = None
    step_started_at: datetime | None = None


@dataclass(frozen=True)
class RestoreObservation:
    """Fresh reads for the current tick; snapshot fields are for state.snapshot."""

    selected_snapshot: str | None = None
    selected_size: str | None = None
    sts_replicas: int = 1
    snapshot_exists: bool = True
    snapshot_ready: bool = True
    check_pvc_phase: str | None = None
    target_pods: int = 0
    pvc_exists: bool = False
    pvc_phase: str | None = None
    old_pv_retain: bool = False
    pod_ready: bool = False


@dataclass(frozen=True)
class RestoreDecision:
    op: RestoreOp
    reason: str


def _restore_to(state: RestoreState, new_state: str, now: datetime) -> RestoreState:
    return replace(state, state=new_state, step_started_at=now)


def restore_step(
    state: RestoreState,
    obs: RestoreObservation,
    now: datetime,
    bind_timeout: int,
    ready_timeout: int,
) -> tuple[RestoreState, RestoreDecision]:
    """Advance the restore one step. Every decision re-checks reality, so a
    restart at any point resumes."""
    def expired(timeout: int) -> bool:
        return (
            state.step_started_at is not None
            and (now - state.step_started_at).total_seconds() > timeout
        )

    def fail(reason: str) -> tuple[RestoreState, RestoreDecision]:
        return _restore_to(state, "RestoreFailed", now), RestoreDecision(
            RestoreOp.FAIL, reason
        )

    if state.state == "Selected":
        if not obs.selected_snapshot:
            return fail("no restorable snapshot")
        return _restore_to(
            replace(
                state,
                snapshot=obs.selected_snapshot,
                size=obs.selected_size,
                original_replicas=obs.sts_replicas,
            ),
            "Validating",
            now,
        ), RestoreDecision(RestoreOp.CHECK_PVC_CREATE, "snapshot selected")

    if state.state == "Validating":
        if not obs.snapshot_exists or not obs.snapshot_ready:
            return fail("snapshot gone or not ready")
        if obs.check_pvc_phase == "Bound":
            return _restore_to(state, "ScalingDown", now), RestoreDecision(
                RestoreOp.CHECK_PVC_DELETE, "check PVC bound, deleting it"
            )
        if expired(bind_timeout):
            return fail(f"check PVC not Bound after {bind_timeout}s")
        if obs.check_pvc_phase is None:
            return state, RestoreDecision(
                RestoreOp.CHECK_PVC_CREATE, "proving the snapshot restores"
            )
        return state, RestoreDecision(RestoreOp.WAIT, "check PVC binding")

    if state.state == "ScalingDown":
        if obs.target_pods == 0:
            return _restore_to(state, "RetainingOldPv", now), RestoreDecision(
                RestoreOp.RETAIN_PV, "pods gone, retaining the old PV"
            )
        return state, RestoreDecision(RestoreOp.SCALE_DOWN, "scaling to 0")

    if state.state == "RetainingOldPv":
        if obs.old_pv_retain:
            return _restore_to(state, "DeletingOldPvc", now), RestoreDecision(
                RestoreOp.PVC_DELETE, "old PV retained, deleting the PVC"
            )
        return state, RestoreDecision(RestoreOp.RETAIN_PV, "setting Retain")

    if state.state == "DeletingOldPvc":
        if not obs.pvc_exists:
            return _restore_to(state, "CreatingPvc", now), RestoreDecision(
                RestoreOp.PVC_CREATE, "old PVC gone, creating from snapshot"
            )
        return state, RestoreDecision(RestoreOp.PVC_DELETE, "deleting old PVC")

    if state.state == "CreatingPvc":
        if obs.pvc_phase == "Bound":
            return _restore_to(state, "ScalingUp", now), RestoreDecision(
                RestoreOp.SCALE_UP, "restored PVC bound, scaling back up"
            )
        if not obs.pvc_exists:
            return state, RestoreDecision(
                RestoreOp.PVC_CREATE, "creating PVC from snapshot"
            )
        if expired(bind_timeout):
            # leave the StatefulSet at 0: scaling up would provision an empty PVC
            return fail(f"PVC not Bound after {bind_timeout}s")
        return state, RestoreDecision(RestoreOp.WAIT, "PVC binding")

    if state.state == "ScalingUp":
        if obs.pod_ready:
            return _restore_to(state, "Restored", now), RestoreDecision(
                RestoreOp.COMPLETE, "pod is Ready"
            )
        if expired(ready_timeout):
            return fail(f"restored pod not Ready after {ready_timeout}s")
        return state, RestoreDecision(RestoreOp.SCALE_UP, "waiting for pod")

    if state.state == "Restored":
        return _restore_to(state, "Done", now), RestoreDecision(
            RestoreOp.CLEAR, "restore done, clearing status"
        )

    return state, RestoreDecision(RestoreOp.WAIT, f"state {state.state}")


def restore_to_status(state: RestoreState) -> dict:
    """Serialize a RestoreState for status.recovery.restore."""
    out = {
        "state": state.state,
        "pvc": state.pvc,
        "pod": state.pod,
        "snapshot": state.snapshot,
        "size": state.size,
        "originalReplicas": state.original_replicas,
        "oldPv": state.old_pv,
        "startedAt": state.started_at.isoformat() if state.started_at else None,
        "stepStartedAt": (
            state.step_started_at.isoformat() if state.step_started_at else None
        ),
    }
    return {k: v for k, v in out.items() if v is not None}


def restore_from_status(d: dict | None) -> RestoreState:
    """Load a RestoreState from status.recovery.restore."""
    d = d or {}
    return RestoreState(
        state=d.get("state"),
        pvc=d.get("pvc"),
        pod=d.get("pod"),
        snapshot=d.get("snapshot"),
        size=d.get("size"),
        original_replicas=d.get("originalReplicas"),
        old_pv=d.get("oldPv"),
        started_at=(
            datetime.fromisoformat(d["startedAt"]) if d.get("startedAt") else None
        ),
        step_started_at=(
            datetime.fromisoformat(d["stepStartedAt"])
            if d.get("stepStartedAt")
            else None
        ),
    )
