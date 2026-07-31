---
title: Helm and Kind
description: A canonical GKE-oriented Helm chart and companion Terraform modules ship in main. Kind local install is not supported.
---

- **Helm chart & Terraform modules.** A canonical GKE-oriented Helm chart (`charts/kube-agents/`) and companion Terraform modules (`terraform/modules/`) ship in `main` for versioned OCI and IaC deployments.
- **No Kind or local-cluster path.** There is no `kind` workflow in the repository, and no scripted installer outside `k8s-operator/scripts/`. You need a real GKE cluster.

## Install today

- [Quick start (GKE)](/kube-agents/install/quickstart-gke/) — `./provision.sh` bootstraps GKE, the operator, and the agent.
- [Helm & Terraform (GitOps)](/kube-agents/deploy/release-versioning/) — deploy via versioned OCI Helm charts and SemVer Terraform modules.
- [Manual install](/kube-agents/install/manual/) — for other Hermes-compatible harnesses.

Check the repository's [`charts/`](https://github.com/gke-labs/kube-agents/tree/main/charts) tree for canonical Helm charts and [`terraform/modules/`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules) for infrastructure modules.
