.DEFAULT_GOAL := help

# must match the csi-snapshotter sidecar in the pinned csi-driver-host-path release
SNAPSHOTTER_VERSION := v8.6.0
HOSTPATH_VERSION := v1.18.0
# pinned csi-driver-nfs release; the lab manifests under lab/ target it
NFS_DRIVER_VERSION := v4.13.5

.PHONY: help cluster cluster-delete csi nfs monitoring lint typecheck test deploy undeploy image graph lint-docs

## Show this help
help:
	@awk '/^## /{desc=substr($$0,4)} /^[a-zA-Z_-]+:/{if(desc){printf "  %-16s %s\n", substr($$1,1,length($$1)-1), desc; desc=""}}' Makefile

## Create the recovery-lab kind cluster
cluster: ; kind create cluster --name recovery-lab --config kind.yaml

## Delete the recovery-lab kind cluster
cluster-delete: ; kind delete cluster --name recovery-lab

## Install the CSI snapshot lab (external-snapshotter + csi-driver-host-path)
csi:
	kubectl apply -k "https://github.com/kubernetes-csi/external-snapshotter/client/config/crd?ref=$(SNAPSHOTTER_VERSION)"
	kubectl -n kube-system apply -k "https://github.com/kubernetes-csi/external-snapshotter/deploy/kubernetes/snapshot-controller?ref=$(SNAPSHOTTER_VERSION)"
	kubectl -n kube-system rollout status deploy/snapshot-controller --timeout=180s
	tmp=$$(mktemp -d); trap 'rm -rf '$$tmp EXIT; \
	git clone --depth 1 --branch $(HOSTPATH_VERSION) https://github.com/kubernetes-csi/csi-driver-host-path "$$tmp" && \
	cd "$$tmp" && deploy/kubernetes-latest/deploy.sh && \
	kubectl apply -f "$$tmp/examples/csi-storageclass.yaml" && \
	kubectl apply -f "$$tmp/examples/csi-volumesnapshotclass.yaml"

## Install csi-driver-nfs and the control-plane NFS server (cross-node volumes)
nfs:
	kubectl apply -f "https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/$(NFS_DRIVER_VERSION)/deploy/$(NFS_DRIVER_VERSION)/rbac-csi-nfs.yaml"
	kubectl apply -f "https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/$(NFS_DRIVER_VERSION)/deploy/$(NFS_DRIVER_VERSION)/csi-nfs-driverinfo.yaml"
	kubectl apply -f "https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/$(NFS_DRIVER_VERSION)/deploy/$(NFS_DRIVER_VERSION)/csi-nfs-controller.yaml"
	kubectl apply -f "https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/$(NFS_DRIVER_VERSION)/deploy/$(NFS_DRIVER_VERSION)/csi-nfs-node.yaml"
	kubectl apply -f lab/nfs-server.yaml
	kubectl rollout status deploy/nfs-server --timeout=180s
	kubectl apply -f lab/nfs-storageclass.yaml

## Deploy Prometheus + Grafana for the recovery lab
# default scrapes the in-cluster operator Service; for a local `kopf run` on the
# host use METRICS_TARGET=172.18.0.1:8000 (the kind network gateway).
# shortcut: unverified on Docker Desktop for Mac — use METRICS_TARGET=host.docker.internal:8000 there.
METRICS_TARGET ?= recovery-operator-metrics.default.svc:8000
monitoring:
	sed 's|__METRICS_TARGET__|$(METRICS_TARGET)|' lab/monitoring.yaml | kubectl apply -f -
	kubectl create configmap recovery-dashboard -n monitoring \
		--from-file=dashboards/recovery.json --dry-run=client -o yaml | kubectl apply -f -
	kubectl -n monitoring rollout status deploy --timeout=180s

## Run ruff
lint: ; ruff check .

## Run mypy
typecheck: ; mypy recovery_operator

## Run pytest
test:
	# shortcut: no tests yet (pytest exit 5), drop this once M1 adds unit tests
	pytest -q || [ $$? -eq 5 ]

## Build the operator image and load it into kind
image:
	docker build -t ghcr.io/iv-cmd/stateful-recovery-operator:latest .
	kind load docker-image ghcr.io/iv-cmd/stateful-recovery-operator:latest --name recovery-lab

## Build the image, apply deploy/, and restart the operator pod
deploy: image
	# kind load keeps the same tag, so a running pod needs an explicit restart
	exists=$$(kubectl get deploy/recovery-operator -o name 2>/dev/null); \
	kubectl apply -f deploy/; \
	if [ -n "$$exists" ]; then kubectl rollout restart deploy/recovery-operator; fi; \
	kubectl rollout status deploy/recovery-operator --timeout=180s

## Remove the operator and its RBAC
undeploy:
	# never the CRD: deleting it deletes every RecoveryPolicy and its data
	kubectl delete -f deploy/operator.yaml --ignore-not-found
	kubectl delete -f deploy/rbac.yaml --ignore-not-found

## Build the knowledge graph into graphify-out/
# graphify CLI: uv tool install graphifyy   or   pipx install graphifyy
graph: ; graphify .

## Lint the prose docs against the STE rules (baseline 0)
lint-docs: ; python scripts/ste-lint.py --baseline 0 README.md LEARNING_LOG.md
