.DEFAULT_GOAL := help

.PHONY: help cluster cluster-delete csi lint typecheck test deploy graph lint-docs

## Show this help
help:
	@awk '/^## /{desc=substr($$0,4)} /^[a-zA-Z_-]+:/{if(desc){printf "  %-16s %s\n", substr($$1,1,length($$1)-1), desc; desc=""}}' Makefile

## Create the recovery-lab kind cluster
cluster: ; kind create cluster --name recovery-lab --config kind.yaml

## Delete the recovery-lab kind cluster
cluster-delete: ; kind delete cluster --name recovery-lab

## Show the CSI lab install steps (installs nothing)
csi:
	@echo "CSI lab steps (run manually, not installed by this target):"
	@echo "  1. Install external-snapshotter CRDs:"
	@echo "     kubectl apply -f https://raw.githubusercontent.com/kubernetes-csi/external-snapshotter/master/client/config/crd"
	@echo "  2. Deploy the snapshot-controller:"
	@echo "     kubectl apply -f https://raw.githubusercontent.com/kubernetes-csi/external-snapshotter/master/deploy/kubernetes/snapshot-controller"
	@echo "  3. Deploy csi-driver-host-path:"
	@echo "     See https://github.com/kubernetes-csi/csi-driver-host-path deploy scripts"
	@echo "  4. Create a VolumeSnapshotClass for the hostpath driver."

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
