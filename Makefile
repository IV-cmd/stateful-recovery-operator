.DEFAULT_GOAL := help

# must match the csi-snapshotter sidecar in the pinned csi-driver-host-path release
SNAPSHOTTER_VERSION := v8.6.0
HOSTPATH_VERSION := v1.18.0
# pinned csi-driver-nfs release; the lab manifests under lab/ target it
NFS_DRIVER_VERSION := v4.13.5

.PHONY: help cluster cluster-delete csi nfs lint typecheck test deploy graph lint-docs

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

## Run ruff
lint: ; ruff check .

## Run mypy
typecheck: ; mypy recovery_operator

## Run pytest
test:
	# shortcut: no tests yet (pytest exit 5), drop this once M1 adds unit tests
	pytest -q || [ $$? -eq 5 ]

## Apply the manifests in deploy/
deploy: ; kubectl apply -f deploy/

## Build the knowledge graph into graphify-out/
# graphify CLI: uv tool install graphifyy   or   pipx install graphifyy
graph: ; graphify .

## Lint the prose docs against the STE rules (baseline 0)
lint-docs: ; python scripts/ste-lint.py --baseline 0 README.md LEARNING_LOG.md
