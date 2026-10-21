"""Failure detection and recovery flow for pod, node, and volume failures."""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum


class Action(Enum):
    RESTART = "RESTART"
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


@dataclass(frozen=True)
class PodMemory:
    """One entry per pod name, stored in status.recovery.pods[name]."""

    uid: str | None = None
    unready_since: datetime | None = None
    restarts: int = 0
    deleted_uid: str | None = None
    dry_run_uid: str | None = None
    escalated: bool = False


@dataclass(frozen=True)
class Policy:
    max_restarts: int
    unready_seconds: int
    max_concurrent: int


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str


def pod_view(pod: dict) -> PodView:
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
    )


def observe(pod: PodView, mem: PodMemory, now: datetime) -> tuple[PodMemory, bool]:
    """Update the memory for this pod. Return (memory, recovered)."""
    recovered = False
    if mem.uid != pod.uid:
        mem = replace(mem, unready_since=None, dry_run_uid=None)
    if pod.ready:
        if mem.deleted_uid or mem.restarts or mem.escalated:
            recovered = True
        mem = replace(mem, unready_since=None, deleted_uid=None, restarts=0, escalated=False)
    elif mem.unready_since is None:
        mem = replace(mem, unready_since=now)
    return replace(mem, uid=pod.uid), recovered


def decide(
    pod: PodView, mem: PodMemory, policy: Policy, in_flight_others: int, now: datetime
) -> Decision:
    """Pick the recovery action for an already-observed pod."""
    if pod.terminating or pod.uid == mem.deleted_uid:
        return Decision(Action.SKIP, "already recovering")
    if pod.ready:
        return Decision(Action.SKIP, "healthy")
    if pod.crash_looping:
        reason = "CrashLoopBackOff"
    else:
        unready_for = (now - mem.unready_since).total_seconds() if mem.unready_since else 0
        if unready_for < policy.unready_seconds:
            return Decision(Action.WAIT, f"unready for {int(unready_for)}s")
        reason = f"unready for {int(unready_for)}s"
    if mem.restarts >= policy.max_restarts:
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
        "dryRunUid": mem.dry_run_uid,
        "escalated": mem.escalated,
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
        dry_run_uid=d.get("dryRunUid"),
        escalated=d.get("escalated", False),
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
