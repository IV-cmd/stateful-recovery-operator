"""Kopf handlers that watch RecoveryPolicy objects and drive backup and recovery."""

import kopf

RESOURCE = ("recovery.iv-cmd.dev", "v1alpha1", "recoverypolicies")


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
