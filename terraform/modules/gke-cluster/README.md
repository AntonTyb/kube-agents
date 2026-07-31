# GKE Autopilot Cluster Module

Reusable Terraform module for provisioning a GKE Autopilot cluster configured for Kube-Agents workloads.

## Usage

```hcl
module "gke_cluster" {
  source       = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/gke-cluster?ref=v1.0.0"
  project_id   = "my-gcp-project"
  cluster_name = "production-host-01"
  location     = "us-central1"
}
```

See the [Release versioning & promotion guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
