# kube-agents Helm Chart

Canonical GKE-oriented Helm chart for deploying the Kube-Agents Kubernetes Operator and Platform Agent Custom Resource.

## Prerequisites

- Kubernetes 1.28+ (GKE Autopilot or Standard)
- Pre-existing Google Service Account (GSA) with Workload Identity binding for `kubeagents-platform-agent` in namespace `kubeagents-system`.

## Usage

```bash
helm repo add kube-agents oci://ghcr.io/gke-labs/kube-agents/charts
helm install kube-agents kube-agents/kube-agents --namespace kubeagents-system --create-namespace \
  --set platformAgent.harness.clusterName=my-cluster \
  --set platformAgent.harness.location=us-central1 \
  --set platformAgent.harness.projectId=my-gcp-project
```

See [docs/site/src/content/docs/deploy/release-versioning.md](../../docs/site/src/content/docs/deploy/release-versioning.md) for versioning rules.
