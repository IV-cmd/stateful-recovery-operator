"""Kopf handlers that watch RecoveryPolicy objects and drive backup and recovery."""

import os
from dataclasses import replace
from datetime import UTC, datetime

import kopf
from kubernetes import client
from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException
from kubernetes.utils import parse_quantity

from . import backup, k8s, metrics, recovery

RESOURCE = ("recovery.iv-cmd.dev", "v1alpha1", "recoverypolicies")


@kopf.on.startup()
def on_startup(**_):
    k8s.load_config()
    metrics.start(int(os.environ.get("METRICS_PORT", "8000")))


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

    rec = status.get("recovery") or {}
    unhealthy = rec.get("restore") or any(
        (p or {}).get("failingSince") for p in (rec.get("pods") or {}).values()
    )
    if unhealthy:
        # never snapshot a possibly corrupted workload
        kopf.info(body, reason="BackupSkipped", message="workload unhealthy")
        return

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
    sts = ApiClient().sanitize_for_serialization(sts_obj)
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
        restore_enabled=(rspec.get("restore") or {}).get("enabled", False),
    )
    dry_run = rspec.get("dryRun", True)
    now = datetime.now(UTC)

    # a restore in progress drives itself; M3 actions stay out of its way
    if rec.get("restore"):
        _restore_drive(
            sts, pods, mems, stored, rec, name, namespace, body, patch, logger, now, rspec
        )
        return

    ready_nodes = {
        n["metadata"]["name"]
        for n in k8s.list_nodes()
        if next(
            (
                c
                for c in (n.get("status") or {}).get("conditions") or []
                if c.get("type") == "Ready"
            ),
            {},
        ).get("status")
        == "True"
    }

    desired: dict[str, dict | None] = {}
    last_action = None
    rec_status_restore = None
    recovered_any = False
    for pod in pods:
        pname = pod["metadata"]["name"]
        node_name = (pod.get("spec") or {}).get("nodeName")
        view = recovery.pod_view(
            pod, node_ready=node_name is None or node_name in ready_nodes
        )
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
                # count the would-be restart so maxRestarts still gates
                mem = replace(mem, dry_run_uid=view.uid, restarts=mem.restarts + 1)
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
        elif d.action is recovery.Action.RESTORE:
            pvc = _pvc_for_pod(sts, pname)
            if dry_run:
                if not mem.restore_dry_run:
                    sel = backup.latest_restorable(
                        backup.list_snapshots(namespace, name, pvc),
                        name,
                        pvc,
                        mem.failing_since or now,
                    )
                    kopf.info(
                        body,
                        reason="RestoreDryRun",
                        message=(
                            f"would restore {pvc} for {pname} from "
                            f"{sel['metadata']['name'] if sel else 'no snapshot found'} "
                            "(validate, scale to 0, retain PV, recreate PVC, scale up)"
                        ),
                    )
                    mem = replace(mem, restore_dry_run=True)
            else:
                state = recovery.RestoreState(
                    state="Selected",
                    pvc=pvc,
                    pod=pname,
                    started_at=now,
                    step_started_at=now,
                )
                kopf.info(
                    body,
                    reason="RestoreStarted",
                    message=f"{pname}: {d.reason}; restoring {pvc} from a snapshot",
                )
                last_action = f"RESTORE {pname}: {d.reason}"
                rec_status_restore = recovery.restore_to_status(state)
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

    if desired != stored or last_action is not None or rec_status_restore:
        rec_status: dict = {"pods": desired}
        if rec_status_restore is not None:
            rec_status["restore"] = rec_status_restore
        if last_action is not None:
            rec_status["lastAction"] = last_action
            rec_status["lastActionTime"] = now.isoformat()
        patch.status["recovery"] = rec_status


def _pvc_for_pod(sts: dict, pod_name: str) -> str:
    """Return the PVC name a pod's volumeClaimTemplate creates."""
    templates = (sts.get("spec") or {}).get("volumeClaimTemplates") or []
    return f"{templates[0]['metadata']['name']}-{pod_name}"


def _pvc_body(sts: dict, name: str, size: str, snapshot: str, ns: str) -> dict:
    """Build a PVC from the StatefulSet template, sourced from a snapshot."""
    templates = (sts.get("spec") or {}).get("volumeClaimTemplates") or []
    tmpl = templates[0].get("spec") or {}
    return {
        "metadata": {"name": name, "labels": dict(backup.MANAGED_BY)},
        "spec": {
            "storageClassName": tmpl.get("storageClassName"),
            "accessModes": tmpl.get("accessModes"),
            "resources": {"requests": {"storage": size}},
            "dataSource": {
                "name": snapshot,
                "kind": "VolumeSnapshot",
                "apiGroup": backup.SNAPSHOT_GROUP,
            },
        },
    }


def _restore_drive(
    sts, pods, mems, stored, rec, name, namespace, body, patch, logger, now, rspec
):
    """Advance the in-progress restore one step per tick."""
    core = client.CoreV1Api()
    apps = client.AppsV1Api()
    custom = client.CustomObjectsApi()
    state = recovery.restore_from_status(rec["restore"])
    restorespec = rspec.get("restore") or {}
    bind_timeout = restorespec.get("bindTimeoutSeconds", 180)
    ready_timeout = restorespec.get("readyTimeoutSeconds", 300)

    def read_pvc(n):
        try:
            return ApiClient().sanitize_for_serialization(
                core.read_namespaced_persistent_volume_claim(n, namespace)
            )
        except ApiException as e:
            if e.status != 404:
                raise
            return None

    sel_name, sel_size = None, None
    if state.state == "Selected":
        before = (mems.get(state.pod) or recovery.PodMemory()).failing_since or now
        sel = backup.latest_restorable(
            backup.list_snapshots(namespace, name, state.pvc), name, state.pvc, before
        )
        if sel:
            sel_name = sel["metadata"]["name"]
            templates = (sts.get("spec") or {}).get("volumeClaimTemplates") or []
            template_size = (
                ((templates[0].get("spec") or {}).get("resources") or {})
                .get("requests", {})
                .get("storage")
            )
            snap_size = (sel.get("status") or {}).get("restoreSize")
            sel_size = (
                snap_size
                if parse_quantity(snap_size or "0")
                >= parse_quantity(template_size or "0")
                else template_size
            )

    snap_exists, snap_ready = True, True
    if state.snapshot:
        try:
            snap = custom.get_namespaced_custom_object(
                backup.SNAPSHOT_GROUP,
                backup.SNAPSHOT_VERSION,
                namespace,
                backup.SNAPSHOT_PLURAL,
                state.snapshot,
            )
            snap_ready = bool((snap.get("status") or {}).get("readyToUse"))
        except ApiException as e:
            if e.status != 404:
                raise
            snap_exists = snap_ready = False

    check = read_pvc(f"{state.pvc}-restore-check") if state.pvc else None
    pvc = read_pvc(state.pvc) if state.pvc else None
    old_pv_retain = False
    if state.old_pv:
        try:
            pv = core.read_persistent_volume(state.old_pv)
            old_pv_retain = pv.spec.persistent_volume_reclaim_policy == "Retain"
        except ApiException as e:
            if e.status != 404:
                raise
    pod_ready = any(
        p["metadata"]["name"] == state.pod
        and recovery.pod_view(p).ready
        for p in pods
    )

    obs = recovery.RestoreObservation(
        selected_snapshot=sel_name,
        selected_size=sel_size,
        sts_replicas=(sts.get("spec") or {}).get("replicas", 1),
        snapshot_exists=snap_exists,
        snapshot_ready=snap_ready,
        check_pvc_phase=(check.get("status") or {}).get("phase") if check else None,
        target_pods=len(pods),
        pvc_exists=pvc is not None,
        pvc_phase=(pvc.get("status") or {}).get("phase") if pvc else None,
        old_pv_retain=old_pv_retain,
        pod_ready=pod_ready,
    )
    prev = state.state
    state, d = recovery.restore_step(
        state, obs, now, bind_timeout, ready_timeout
    )

    if state.state != prev and d.op not in (
        recovery.RestoreOp.FAIL,
        recovery.RestoreOp.COMPLETE,
        recovery.RestoreOp.CLEAR,
    ):
        kopf.info(body, reason="RestoreStep", message=state.state)

    if d.op is recovery.RestoreOp.CHECK_PVC_CREATE:
        try:
            core.create_namespaced_persistent_volume_claim(
                namespace,
                _pvc_body(sts, f"{state.pvc}-restore-check", state.size, state.snapshot, namespace),
            )
        except ApiException as e:
            if e.status != 409:
                raise
    elif d.op is recovery.RestoreOp.CHECK_PVC_DELETE:
        try:
            core.delete_namespaced_persistent_volume_claim(
                f"{state.pvc}-restore-check", namespace
            )
        except ApiException as e:
            if e.status != 404:
                raise
    elif d.op is recovery.RestoreOp.SCALE_DOWN:
        apps.patch_namespaced_stateful_set(
            sts["metadata"]["name"], namespace, {"spec": {"replicas": 0}}
        )
    elif d.op is recovery.RestoreOp.RETAIN_PV:
        if state.old_pv is None and pvc:
            pv_name = (pvc.get("spec") or {}).get("volumeName")
            core.patch_persistent_volume(
                pv_name,
                {
                    "spec": {"persistentVolumeReclaimPolicy": "Retain"},
                    "metadata": {
                        "annotations": {
                            "recovery.iv-cmd.dev/retained-from": f"{namespace}/{state.pvc}"
                        }
                    },
                },
            )
            state = replace(state, old_pv=pv_name)
    elif d.op is recovery.RestoreOp.PVC_DELETE:
        try:
            core.delete_namespaced_persistent_volume_claim(state.pvc, namespace)
        except ApiException as e:
            if e.status != 404:
                raise
    elif d.op is recovery.RestoreOp.PVC_CREATE:
        try:
            core.create_namespaced_persistent_volume_claim(
                namespace,
                _pvc_body(sts, state.pvc, state.size, state.snapshot, namespace),
            )
        except ApiException as e:
            if e.status != 409:
                raise
    elif d.op is recovery.RestoreOp.SCALE_UP:
        apps.patch_namespaced_stateful_set(
            sts["metadata"]["name"],
            namespace,
            {"spec": {"replicas": state.original_replicas or 1}},
        )
    elif d.op is recovery.RestoreOp.COMPLETE:
        duration = int((now - state.started_at).total_seconds()) if state.started_at else 0
        kopf.info(
            body,
            reason="RestoreCompleted",
            message=f"{state.pvc} restored from {state.snapshot} in {duration}s",
        )
        patch.status["phase"] = "Ready"
        patch.status["lastRestoreTime"] = now.isoformat()
        patch.status["lastRestoreSnapshot"] = state.snapshot
    elif d.op is recovery.RestoreOp.FAIL:
        # a failure from any state cleans up the validation PVC
        try:
            core.delete_namespaced_persistent_volume_claim(
                f"{state.pvc}-restore-check", namespace
            )
        except ApiException as e:
            if e.status != 404:
                raise
        kopf.warn(
            body,
            reason="RestoreFailed",
            message=(
                f"{d.reason}; snapshot={state.snapshot or 'none'}, "
                f"retained PV={state.old_pv or 'none'}"
            ),
        )
        patch.status["phase"] = "Failed"
    else:
        logger.debug(f"{name}: restore {d.op.value} {d.reason}")

    if d.op is recovery.RestoreOp.COMPLETE and state.pod in mems:
        mem = replace(
            mems[state.pod],
            restarts=0,
            deleted_uid=None,
            unready_since=None,
            failing_since=None,
            escalated=False,
            restored=True,
        )
        entry = recovery.to_status(mem)
        for k in (stored.get(state.pod) or {}):
            entry.setdefault(k, None)
        pods_status = dict(stored)
        pods_status[state.pod] = entry
        rec_status = dict(rec)
        rec_status["pods"] = pods_status
    else:
        rec_status = dict(rec)

    new_restore = recovery.restore_to_status(state)
    if d.op is recovery.RestoreOp.CLEAR:
        new_restore = None
    if new_restore is not None:
        for k in rec["restore"]:
            new_restore.setdefault(k, None)
    rec_status["restore"] = new_restore
    patch.status["recovery"] = rec_status


def _node_view(node: dict, fenced_by: str) -> recovery.NodeView:
    meta = node["metadata"]
    status = node.get("status") or {}
    ready: dict = next(
        (c for c in status.get("conditions") or [] if c.get("type") == "Ready"), {}
    )
    taints = (node.get("spec") or {}).get("taints") or []
    labels = meta.get("labels") or {}
    annotations = meta.get("annotations") or {}
    return recovery.NodeView(
        name=meta["name"],
        uid=meta["uid"],
        ready=ready.get("status") == "True",
        ready_since_transition=(
            datetime.fromisoformat(ready["lastTransitionTime"])
            if ready.get("lastTransitionTime")
            else None
        ),
        unreachable_taint=any(t.get("key") == "node.kubernetes.io/unreachable" for t in taints),
        oos_taint=any(t.get("key") == k8s.OOS_TAINT_KEY for t in taints),
        fenced_by_us=annotations.get(k8s.FENCED_BY_KEY) == fenced_by,
        control_plane="node-role.kubernetes.io/control-plane" in labels,
    )


def _any_attach_required(target_pvcs: set) -> bool:
    """Return True if any target PVC's PV driver requires VolumeAttachments."""
    drivers = set()
    for pv in k8s.list_persistent_volumes():
        claim = ((pv.get("spec") or {}).get("claimRef") or {}).get("name")
        driver = ((pv.get("spec") or {}).get("csi") or {}).get("driver")
        if claim in target_pvcs and driver:
            drivers.add(driver)
    for driver in drivers:
        try:
            csi = client.StorageV1Api().read_csi_driver(driver)
        except ApiException:
            continue
        if csi.spec.attach_required:
            return True
    return False


@kopf.timer(*RESOURCE, interval=10, initial_delay=5)
def fencing_timer(spec, status, name, namespace, body, patch, logger, **_):
    rspec = spec.get("recovery") or {}
    fspec = rspec.get("fencing") or {}
    policy = recovery.FencePolicy(
        enabled=fspec.get("enabled", False),
        dry_run=rspec.get("dryRun", True),
        not_ready_seconds=fspec.get("notReadySeconds", 120),
        verify_seconds=fspec.get("verifySeconds", 180),
    )

    sts_name = spec["targetRef"]["name"]
    try:
        sts = client.AppsV1Api().read_namespaced_stateful_set(sts_name, namespace)
    except ApiException as e:
        if e.status != 404:
            raise
        return
    sts = ApiClient().sanitize_for_serialization(sts)
    labels = sts.get("spec", {}).get("selector", {}).get("matchLabels") or {}
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
    target_pvcs = set(backup.pvc_names(sts))

    stored = (status.get("recovery") or {}).get("fencing") or {}
    mems = {n: recovery.fence_from_status(d) for n, d in stored.items()}
    fenced_by = f"{namespace}/{name}"
    now = datetime.now(UTC)

    node_names = set()
    for p in pods:
        if (p.get("spec") or {}).get("nodeName"):
            node_names.add(p["spec"]["nodeName"])
    node_names |= set(stored)
    if not node_names:
        return

    all_nodes = {n["metadata"]["name"]: n for n in k8s.list_nodes()}
    unhealthy = {
        n["metadata"]["name"]
        for n in all_nodes.values()
        if next(
            (c for c in (n.get("status") or {}).get("conditions") or [] if c.get("type") == "Ready"),
            {},
        ).get("status") != "True"
    }
    pv_to_pvc = {
        pv["metadata"]["name"]: ((pv.get("spec") or {}).get("claimRef") or {}).get("name")
        for pv in k8s.list_persistent_volumes()
    }
    attachments = [
        a
        for a in k8s.list_volume_attachments()
        if pv_to_pvc.get((a.get("spec") or {}).get("source", {}).get("persistentVolumeName"))
        in target_pvcs
    ]

    desired: dict[str, dict | None] = {}
    for node_name in node_names:
        node = all_nodes.get(node_name)
        if node is None:
            desired[node_name] = None
            continue
        view = _node_view(node, fenced_by)
        on_node = [
            p for p in pods if (p.get("spec") or {}).get("nodeName") == node_name
        ]
        left = sum(
            1 for a in attachments if (a.get("spec") or {}).get("nodeName") == node_name
        )
        sit = recovery.NodeSituation(
            target_pods_on_node=len(on_node),
            target_pods_gone=len(on_node) == 0,
            attachments_left=left,
            other_unhealthy_nodes=len(unhealthy - {node_name}),
        )
        mem = mems.get(node_name, recovery.FenceMemory())
        mem, d = recovery.decide_node(view, mem, sit, policy, now)

        if d.action is recovery.NodeAction.FENCE:
            k8s.fence_node(node_name, fenced_by)
            mem = replace(mem, state="Fenced")
            kopf.info(body, reason="NodeFenced", message=f"{node_name}: {d.reason}")
        elif d.action is recovery.NodeAction.DRY_RUN:
            kopf.info(
                body,
                reason="NodeFenceDryRun",
                message=f"would taint {node_name} out-of-service",
            )
        elif d.action is recovery.NodeAction.VERIFY_OK:
            seconds = (
                int((now - mem.fenced_at).total_seconds()) if mem.fenced_at else 0
            )
            note = (
                "attachments cleared"
                if _any_attach_required(target_pvcs)
                else "no VolumeAttachment required"
            )
            kopf.info(
                body,
                reason="FenceVerified",
                message=f"{node_name}: verified after {seconds}s, {note}",
            )
        elif d.action is recovery.NodeAction.VERIFY_TIMEOUT:
            kopf.warn(body, reason="FenceUnverified", message=f"{node_name}: {d.reason}")
        elif d.action is recovery.NodeAction.REFUSE:
            kopf.warn(body, reason="FenceRefused", message=f"{node_name}: {d.reason}")
        elif d.action is recovery.NodeAction.UNFENCE:
            k8s.unfence_node(node_name)
            kopf.info(body, reason="NodeUnfenced", message=f"{node_name}: taint removed")
        else:
            logger.debug(f"{name}/{node_name}: {d.action.value} {d.reason}")

        desired[node_name] = recovery.fence_to_status(mem) if mem.state else None

    for n in stored:
        if n not in desired:
            desired[n] = None
    for n, entry in desired.items():
        if entry is not None and n in stored:
            for k in stored[n]:
                entry.setdefault(k, None)

    if desired != stored:
        rec_status = dict(status.get("recovery") or {})
        rec_status["fencing"] = desired
        patch.status["recovery"] = rec_status
