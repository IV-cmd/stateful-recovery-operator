# stateful-recovery-operator

A Kopf-based Kubernetes operator. It watches a RecoveryPolicy CRD. It schedules
CSI VolumeSnapshot backups of StatefulSet PVCs. It recovers StatefulSets from
pod, node, and volume failures. It fences dead nodes with the
node.kubernetes.io/out-of-service taint.

This is a learning project. The owner writes all implementation code.

## Repository layout

- `recovery_operator/` — operator source code (Kopf handlers and helpers)
- `deploy/` — Kubernetes manifests for the operator
- `examples/` — example manifests (StatefulSet, RecoveryPolicy)
- `dashboards/` — Grafana dashboards
- `tests/unit/` — unit tests
- `tests/e2e/` — end-to-end tests against a kind cluster
- `scripts/` — helper scripts, including the STE linter
- `kind.yaml` — kind cluster config for local development

## Quick start

1. Run `make cluster` to create the kind cluster.
2. Run `make csi` to install the CSI snapshot lab.
3. Run `make nfs` to install the NFS driver and server. It provides cross-node volumes for the M4 fencing test.
4. Run `make deploy` to apply the manifests in `deploy/`.

## Architecture

TODO: document the controller flow once M1 lands.

## Measured recovery times

TODO: fill in after the e2e milestones run.

## RBAC trade-offs

TODO: document the permissions the operator needs and why.

## Known limitations

TODO: document limits as they appear.
