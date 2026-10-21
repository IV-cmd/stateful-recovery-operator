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
