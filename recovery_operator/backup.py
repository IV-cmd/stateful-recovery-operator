"""CSI VolumeSnapshot scheduling and lifecycle for StatefulSet PVCs."""

from datetime import datetime, timedelta
from typing import Any, cast

from croniter import croniter
from kubernetes import client
from kubernetes.client.exceptions import ApiException

SNAPSHOT_GROUP, SNAPSHOT_VERSION, SNAPSHOT_PLURAL = (
    "snapshot.storage.k8s.io",
    "v1",
    "volumesnapshots",
)
SNAPSHOT_CLASS = "csi-hostpath-snapclass"
MANAGED_BY = {"app.kubernetes.io/managed-by": "recovery-operator"}
POLICY_LABEL = "recovery.iv-cmd.dev/policy"
PVC_LABEL = "recovery.iv-cmd.dev/pvc"


def current_slot(schedule: str, now: datetime) -> datetime:
    """Return the latest cron fire time at or before now."""
    # croniter.get_prev excludes an exact boundary (14:30:00 -> 14:28 for */2),
    # so start the search one minute into the future.
    start = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return croniter(schedule, start).get_prev(datetime)


def is_due(last_backup: str | None, slot: datetime) -> bool:
    """Return True when no backup exists for the slot."""
    if last_backup is None:
        return True
    return datetime.fromisoformat(last_backup) < slot


def pvc_names(sts: dict) -> list[str]:
    """Return the PVC names a StatefulSet creates, in dict form."""
    name = sts["metadata"]["name"]
    replicas = sts.get("spec", {}).get("replicas", 1)
    templates = sts.get("spec", {}).get("volumeClaimTemplates", [])
    return [
        f"{tmpl['metadata']['name']}-{name}-{i}"
        for tmpl in templates
        for i in range(replicas)
    ]


def snapshot_name(policy: str, pvc: str, slot: datetime) -> str:
    """Return the deterministic VolumeSnapshot name for a slot."""
    return f"{policy}-{pvc}-{slot:%Y%m%d%H%M}"


def snapshot_body(name: str, policy: str, pvc: str) -> dict:
    """Return the VolumeSnapshot manifest for one PVC and slot."""
    return {
        "apiVersion": f"{SNAPSHOT_GROUP}/{SNAPSHOT_VERSION}",
        "kind": "VolumeSnapshot",
        "metadata": {
            "name": name,
            "labels": {**MANAGED_BY, POLICY_LABEL: policy, PVC_LABEL: pvc},
        },
        "spec": {
            "volumeSnapshotClassName": SNAPSHOT_CLASS,
            "source": {"persistentVolumeClaimName": pvc},
        },
    }


def is_pending(snap: dict) -> bool:
    """Return True while a snapshot has neither finished nor failed."""
    status = snap.get("status") or {}
    return not status.get("readyToUse") and not status.get("error")


def snapshots_to_prune(snaps: list[dict], retention: int) -> list[str]:
    """Return the names of all but the newest retention snapshots."""
    ordered = sorted(
        snaps,
        key=lambda s: (s["metadata"]["creationTimestamp"], s["metadata"]["name"]),
    )
    return [s["metadata"]["name"] for s in ordered[: max(len(ordered) - retention, 0)]]


def list_snapshots(namespace: str, policy: str, pvc: str | None = None) -> list[dict]:
    """List this policy's VolumeSnapshots, optionally for one PVC."""
    selector = f"{POLICY_LABEL}={policy}"
    if pvc is not None:
        selector += f",{PVC_LABEL}={pvc}"
    result = client.CustomObjectsApi().list_namespaced_custom_object(
        SNAPSHOT_GROUP, SNAPSHOT_VERSION, namespace, SNAPSHOT_PLURAL,
        label_selector=selector,
    )
    return cast(dict[str, Any], result)["items"]


def create_snapshot(namespace: str, body: dict) -> bool:
    """Create a VolumeSnapshot. Return False when the name already exists."""
    try:
        client.CustomObjectsApi().create_namespaced_custom_object(
            SNAPSHOT_GROUP, SNAPSHOT_VERSION, namespace, SNAPSHOT_PLURAL, body,
        )
        return True
    except ApiException as e:
        if e.status == 409:
            return False
        raise


def delete_snapshot(namespace: str, name: str) -> None:
    """Delete a VolumeSnapshot, ignoring a missing one."""
    try:
        client.CustomObjectsApi().delete_namespaced_custom_object(
            SNAPSHOT_GROUP, SNAPSHOT_VERSION, namespace, SNAPSHOT_PLURAL, name,
        )
    except ApiException as e:
        if e.status != 404:
            raise
