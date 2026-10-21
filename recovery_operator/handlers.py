"""Kopf handlers that watch RecoveryPolicy objects and drive backup and recovery."""

from dataclasses import replace
from datetime import UTC, datetime

import kopf
from kubernetes import client
from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException

from . import backup, k8s, recovery

RESOURCE = ("recovery.iv-cmd.dev", "v1alpha1", "recoverypolicies")


@kopf.on.startup()
def on_startup(**_):
    k8s.load_config()


@kopf.on.create(*RESOURCE)
def on_create(spec, name, namespace, patch, logger, **_):
    logger.info(f"RecoveryPolicy {namespace}/{name} created for StatefulSet {spec['targetRef']['name']}")
    patch.status["phase"] = "Ready"


@kopf.on.update(*RESOURCE)
def on_update(diff, name, namespace, patch, logger, **_):
    logger.info(f"RecoveryPolicy {namespace}/{name} updated: {list(diff)}")
    patch.status["phase"] = "Ready"


@kopf.on.delete(*RESOURCE)
def on_delete(name, namespace, patch, logger, **_):
    # Visible only while cleanup runs or retries; the object is gone once Kopf drops its finalizer.
    logger.info(f"RecoveryPolicy {namespace}/{name} deleted")
    patch.status["phase"] = "Deleting"


@kopf.timer(*RESOURCE, interval=60, initial_delay=5)
def backup_timer(spec, status, name, namespace, body, patch, logger, **_):
    schedule = spec.get("backup", {}).get("schedule")
    if not schedule:
        return
    slot = backup.current_slot(schedule, datetime.now(UTC))
    if not backup.is_due(status.get("lastBackupTime"), slot):
        logger.debug(f"{name}: slot {slot.isoformat()} already backed up")
        return

    sts_name = spec["targetRef"]["name"]
    try:
        sts = client.AppsV1Api().read_namespaced_stateful_set(sts_name, namespace)
    except ApiException as e:
        if e.status != 404:
            raise
        kopf.warn(body, reason="TargetNotFound", message=f"StatefulSet {sts_name} not found")
        patch.status["phase"] = "TargetNotFound"
        return
    sts = ApiClient().sanitize_for_serialization(sts)

    if any(backup.is_pending(s) for s in backup.list_snapshots(namespace, name)):
        logger.info(f"{name}: a snapshot is still pending, skipping this tick")
        kopf.info(body, reason="BackupSkipped", message="a snapshot is still pending")
        return

    for pvc in backup.pvc_names(sts):
        snap_name = backup.snapshot_name(name, pvc, slot)
        if backup.create_snapshot(namespace, backup.snapshot_body(snap_name, name, pvc)):
            kopf.info(body, reason="BackupCreated", message=snap_name)
        else:
            logger.info(f"{snap_name} already exists, skipping")

    retention = spec.get("backup", {}).get("retention")
    if retention:
        for pvc in backup.pvc_names(sts):
            snaps = backup.list_snapshots(namespace, name, pvc)
            pending = {s["metadata"]["name"] for s in snaps if backup.is_pending(s)}
            for snap_name in backup.snapshots_to_prune(snaps, retention):
                if snap_name in pending:
                    continue  # never delete an in-flight snapshot
                try:
                    backup.delete_snapshot(namespace, snap_name)
                except ApiException as e:
                    kopf.warn(body, reason="PruneFailed", message=f"{snap_name}: {e.reason}")
                    continue
                kopf.info(body, reason="BackupPruned", message=snap_name)

    patch.status["lastBackupTime"] = slot.isoformat()
    if status.get("phase") != "Failed":
        patch.status["phase"] = "Ready"


@kopf.timer(*RESOURCE, interval=10, initial_delay=5)
def recovery_timer(spec, status, name, namespace, body, patch, logger, **_):
    sts_name = spec["targetRef"]["name"]
    try:
        sts_obj = client.AppsV1Api().read_namespaced_stateful_set(sts_name, namespace)
    except ApiException as e:
        if e.status != 404:
            raise
        return
    labels = sts_obj.spec.selector.match_labels or {}
    selector = ",".join(f"{k}={v}" for k, v in labels.items())
    pods = client.CoreV1Api().list_namespaced_pod(namespace, label_selector=selector)
    pods = [
        ApiClient().sanitize_for_serialization(p)
        for p in pods.items
        if any(
            o.kind == "StatefulSet" and o.name == sts_name
            for o in (p.metadata.owner_references or [])
        )
    ]

    rec = status.get("recovery") or {}
    stored = rec.get("pods") or {}
    mems = {n: recovery.from_status(d) for n, d in stored.items()}
    rspec = spec.get("recovery") or {}
    policy = recovery.Policy(
        rspec.get("maxRestarts", 3),
        rspec.get("unreadySeconds", 120),
        rspec.get("maxConcurrentRecoveries", 1),
    )
    dry_run = rspec.get("dryRun", True)
    now = datetime.now(UTC)

    desired: dict[str, dict | None] = {}
    last_action = None
    recovered_any = False
    for pod in pods:
        pname = pod["metadata"]["name"]
        view = recovery.pod_view(pod)
        mem, recovered = recovery.observe(
            view, mems.get(pname, recovery.PodMemory()), now
        )
        if recovered:
            recovered_any = True
            kopf.info(body, reason="RecoveryCompleted", message=f"{pname} is Ready")
        in_flight = sum(
            1 for n, m in mems.items() if n != pname and m.deleted_uid
        )
        d = recovery.decide(view, mem, policy, in_flight, now)
        if d.action is recovery.Action.RESTART:
            if dry_run:
                if mem.dry_run_uid != view.uid:
                    kopf.info(
                        body,
                        reason="RecoveryDryRun",
                        message=f"would delete {pname}: {d.reason}",
                    )
                    mem = replace(mem, dry_run_uid=view.uid)
            else:
                try:
                    client.CoreV1Api().delete_namespaced_pod(pname, namespace)
                except ApiException as e:
                    if e.status != 404:
                        raise
                kopf.info(
                    body,
                    reason="RecoveryStarted",
                    message=f"deleted {pname}: {d.reason}",
                )
                mem = replace(mem, deleted_uid=view.uid, restarts=mem.restarts + 1)
                last_action = f"RESTART {pname}: {d.reason}"
        elif d.action is recovery.Action.ESCALATE:
            if not mem.escalated:
                kopf.warn(body, reason="RecoveryEscalated", message=f"{pname}: {d.reason}")
                mem = replace(mem, escalated=True)
                patch.status["phase"] = "Failed"
                last_action = f"ESCALATE {pname}: {d.reason}"
        else:
            logger.debug(f"{name}/{pname}: {d.action.value} {d.reason}")
        mems[pname] = mem
        desired[pname] = recovery.to_status(mem)

    if recovered_any and not any(m.escalated for m in mems.values()):
        patch.status["phase"] = "Ready"

    # pods that no longer exist are cleared by the merge patch's null value
    for n in stored:
        if n not in desired:
            desired[n] = None
    # the merge patch merges pod entries, so cleared fields need explicit nulls
    for n, entry in desired.items():
        if entry is not None and n in stored:
            for k in stored[n]:
                entry.setdefault(k, None)

    if desired != stored or last_action is not None:
        rec_status: dict = {"pods": desired}
        if last_action is not None:
            rec_status["lastAction"] = last_action
            rec_status["lastActionTime"] = now.isoformat()
        patch.status["recovery"] = rec_status
