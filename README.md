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

## After RestoreFailed

The operator does not retry a failed restore. Inspect these items before you reset:

- The snapshot named in the `RestoreFailed` event and in `status.recovery.restore.snapshot`.
- The retained old PV, if any. Its name is in `status.recovery.restore.oldPv` and it carries the annotation `recovery.iv-cmd.dev/retained-from`. It keeps the pre-restore data.
- The StatefulSet replica count. A failure in `CreatingPvc` leaves it at 0 by design.

To clear the failed state and let the operator try again:

```sh
kubectl patch rp <name> --subresource=status --type=merge -p '{"status":{"phase":"Ready","recovery":{"restore":null}}}'
```

## Observability

Run `make monitoring` to install Prometheus and Grafana in the `monitoring` namespace. Both run on the control-plane node, so a fence on a worker can't evict them. The operator serves metrics on port 8000 (`METRICS_PORT`). Prometheus scrapes it at the kind gateway `172.18.0.1:8000`. On Docker Desktop, run `make monitoring METRICS_TARGET=host.docker.internal:8000` instead (untested).

The dashboard `dashboards/recovery.json` loads automatically. Port-forward `svc/grafana 3000` to open it. The screenshot shows one restart, one restore, and one fence:

![Recovery dashboard after a restart, a restore, and a fence](dashboards/recovery.png)

Useful queries:

- `time() - backup_last_success_timestamp_seconds`: the age of the newest ready snapshot, which is the data you would lose now.
- `histogram_quantile(0.5, sum by (le, action) (rate(recovery_duration_seconds_bucket[1h])))`: the median recovery time per action.
- `sum by (action, result) (recovery_actions_total)`: the count of each recovery outcome.

## Architecture

TODO: document the controller flow once M1 lands.

## Measured recovery times

Measured on kind with the operator's `recovery_duration_seconds` histogram:

| Action | Measured from | Time |
|---|---|---|
| Pod restart | Pod delete to Ready | 10s |
| Node fence | Taint to verified | 10–20s |
| Restore | Restore start to pod Ready | 80s |

A node fence also waits about 2 minutes after the node stops: about 48s until Kubernetes marks it NotReady, then `notReadySeconds`. With backups every 2 minutes, the backup age stays below 4 minutes.

## RBAC trade-offs

TODO: document the permissions the operator needs and why.

## Known limitations

TODO: document limits as they appear.
