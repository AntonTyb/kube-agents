---
title: Release versioning & promotion
description: How Kube-Agents Release Candidate builds are promoted to immutable SemVer releases across container images, Helm charts, and Terraform modules.
sidebar:
  order: 4
---

`kube-agents` follows strict [Semantic Versioning 2.0.0](https://semver.org/) (`vMAJOR.MINOR.PATCH`) for production releases across Docker images, OCI Helm charts, and Terraform modules.

## Promotion from Release Candidate (RC) to SemVer

1. **RC Testing**: Pre-release builds use `rc_YYMMDDHHMM_<short_sha>`. Once end-to-end suite validation succeeds, the commit receives the `*_validated` tag.
2. **SemVer Publication**: Tagging a commit with `vX.Y.Z` triggers GitHub Actions to publish immutable artifacts:
   - **GHCR Images**: `ghcr.io/gke-labs/kube-agents/platform-agent:v1.2.0`
   - **OCI Helm Charts**: `oci://ghcr.io/gke-labs/kube-agents/charts/kube-agents:1.2.0`
   - **Terraform Modules**: Sourced via Git tag reference `?ref=v1.2.0`

## Helm Chart Versioning Matrix

| Chart `version` | Chart `appVersion` | Trigger Condition                                           |
| :-------------- | :----------------- | :---------------------------------------------------------- |
| `1.0.0`         | `v1.0.0`           | Initial production release                                  |
| `1.0.1`         | `v1.0.0`           | Chart template or documentation bugfix (no image change)    |
| `1.1.0`         | `v1.1.0`           | Application release with new features and updated image tag |

## Pinning Terraform Module Versions in GitOps

When forking the GitOps reference repository (`examples/gitops-repo/`), source Terraform modules using explicit SemVer Git tags:

```hcl
module "gke_cluster" {
  source       = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/gke-cluster?ref=v1.2.0"
  project_id   = var.project_id
  cluster_name = "production-host-01"
}
```
