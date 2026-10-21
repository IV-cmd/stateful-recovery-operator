"""Kubernetes API helpers shared by the other modules."""

import kubernetes.config
from kubernetes import client
from kubernetes.client import ApiClient

OOS_TAINT_KEY = "node.kubernetes.io/out-of-service"
OOS_TAINT_VALUE = "nodeshutdown"
OOS_TAINT_EFFECT = "NoExecute"
FENCED_BY_KEY = "recovery.iv-cmd.dev/fenced-by"


def load_config() -> None:
    """Load the in-cluster config, or the local kubeconfig outside a pod."""
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()


def sanitize(obj) -> dict:
    """Return a client object as a plain dict."""
    return ApiClient().sanitize_for_serialization(obj)


def read_node(name: str) -> dict:
    """Read one node as a dict."""
    return sanitize(client.CoreV1Api().read_node(name))


def list_nodes() -> list[dict]:
    """List all nodes as dicts."""
    return [sanitize(n) for n in client.CoreV1Api().list_node().items]


def fence_node(name: str, fenced_by: str) -> None:
    """Apply the out-of-service taint and the fenced-by annotation.

    Strategic merge patch: taints merge on their key, so existing taints from
    other controllers survive.
    """
    body = {
        "metadata": {"annotations": {FENCED_BY_KEY: fenced_by}},
        "spec": {
            "taints": [
                {
                    "key": OOS_TAINT_KEY,
                    "value": OOS_TAINT_VALUE,
                    "effect": OOS_TAINT_EFFECT,
                }
            ]
        },
    }
    client.CoreV1Api().patch_node(name, body)


def unfence_node(name: str) -> None:
    """Remove only our taint and our annotation via read-modify-write.

    A merge patch cannot drop a list item, so the resourceVersion pins the
    update; the caller treats a conflict as a normal retry.
    """
    api = client.CoreV1Api()
    node = api.read_node(name)
    if node.spec:
        node.spec.taints = [
            t for t in (node.spec.taints or []) if t.key != OOS_TAINT_KEY
        ]
    if node.metadata and node.metadata.annotations:
        node.metadata.annotations.pop(FENCED_BY_KEY, None)
    api.replace_node(name, node)


def list_volume_attachments() -> list[dict]:
    """List all VolumeAttachments as dicts."""
    return [
        sanitize(a)
        for a in client.StorageV1Api().list_volume_attachment().items
    ]


def list_persistent_volumes() -> list[dict]:
    """List all PersistentVolumes as dicts."""
    return [sanitize(pv) for pv in client.CoreV1Api().list_persistent_volume().items]
