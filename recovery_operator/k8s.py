"""Kubernetes API helpers shared by the other modules."""

import kubernetes.config


def load_config() -> None:
    """Load the in-cluster config, or the local kubeconfig outside a pod."""
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()
