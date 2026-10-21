"""Kopf handlers that watch RecoveryPolicy objects and drive backup and recovery."""

from datetime import UTC, datetime

import kopf
from kubernetes import client
from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException

from . import backup, k8s

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

    patch.status["lastBackupTime"] = slot.isoformat()
    patch.status["phase"] = "Ready"
