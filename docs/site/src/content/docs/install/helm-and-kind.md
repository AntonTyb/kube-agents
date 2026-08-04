---
title: Helm and Kind
description: A canonical GKE-oriented Helm chart and companion Terraform modules live in main. Kind local install is not supported.
---

- **Helm chart & Terraform modules.** A canonical GKE-oriented Helm chart (`charts/kube-agents/`) and companion Terraform modules (`terraform/modules/`) live in `main` for versioned OCI and IaC deployments. Published artifacts (the OCI chart and `?ref=vX.Y.Z` module tags) only exist from the first `vX.Y.Z` release tag onward — until then, install from a repository checkout or use the [Quick start](/kube-agents/install/quickstart-gke/). A checkout install must override both image tags (`--set operator.image.tag=latest --set platformAgent.deployment.image.tag=latest`, or a commit SHA): a checkout's placeholder `appVersion` never matches a published image tag, so the chart's defaults would otherwise pull images that don't exist.
- **No Kind or local-cluster path.** There is no `kind` workflow in the repository, and no scripted installer outside `k8s-operator/scripts/`. You need a real GKE cluster.

## Install today

- [Quick start (GKE)](/kube-agents/install/quickstart-gke/) — `./provision.sh` bootstraps GKE, the operator, and the agent.
- [Helm & Terraform (GitOps)](/kube-agents/deploy/release-versioning/) — deploy via versioned OCI Helm charts and SemVer Terraform modules.
- [Manual install](/kube-agents/install/manual/) — for other Hermes-compatible harnesses.

Check the repository's [`charts/`](https://github.com/gke-labs/kube-agents/tree/main/charts) tree for canonical Helm charts and [`terraform/modules/`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules) for infrastructure modules.
