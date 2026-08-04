# Full install (Terraform root composition)

A single `terraform apply` that provisions everything a running Platform Agent
needs — the IaC counterpart of the interactive
[`k8s-operator/scripts/provision.sh`](../../../k8s-operator/scripts/provision.sh)
flow. Use one or the other per project, not both: they would fight over the
same cluster, service accounts, and IAM bindings.

## What it provisions

- The required Google APIs (`google_project_service`, never disabled on
  destroy), including the Chat and KMS APIs only when the matching feature is
  enabled.
- A GKE Autopilot cluster ([`gke-cluster`](../../modules/gke-cluster) module)
  with Workload Identity enabled.
- The agent's GCP identity ([`kube-agents-iam`](../../modules/kube-agents-iam)
  module): the `kubeagents-platform-gsa` service account, its read-only
  project roles, and the Workload Identity binding to the
  `kubeagents-platform-agent` KSA.
- Optionally (`enable_google_chat = true`) the Google Chat backend
  ([`chat-pubsub`](../../modules/chat-pubsub) module): Pub/Sub topic,
  subscription, and Chat integration wiring.
- Optionally (`enable_github_minter = true`) the GitHub token minter backend
  ([`github-minter`](../../modules/github-minter) module): minter service
  account plus a KMS key ring and signing key.
- The [`kube-agents` Helm chart](../../../charts/kube-agents) (operator +
  `PlatformAgent` CR) via `helm_release`, installed straight from this
  repository checkout with Workload Identity annotations and the credentials
  Secret composed from your variables.

## Prerequisites

- A GCP project you can administer.
- Terraform `~> 1.5`.
- Application Default Credentials for the Google, Kubernetes, and Helm
  providers:

  ```bash
  gcloud auth application-default login
  ```

## Usage

```bash
cd terraform/examples/full-install
cp terraform.tfvars.example terraform.tfvars   # then edit it
terraform init
terraform apply
```

### The `image_tag` rule

`image_tag` (default `latest`) overrides both the operator and platform-agent
image tags. It exists because the chart is installed from this checkout, and a
checkout's `Chart.yaml` carries an `appVersion` placeholder that never matches
a published image tag — so the chart's usual tag defaulting cannot work here
(see the [chart README](../../../charts/kube-agents/README.md)). `latest` is
fine for evaluation; pin a `vX.Y.Z` release tag for production.

### Google Chat caveat: manual CR wiring

The chart's `PlatformAgent` CR template does not yet expose the `googleChat`
integration values, so with `enable_google_chat = true` this composition can
only provision the GCP backend (topic, subscription, IAM). Wiring the CR's
`googleChat` section is chart follow-up scope; until then, patch the CR
manually with the `chat_topic_name` and `chat_subscription_name` outputs.

## Standalone use outside this repository

This example sources the modules by relative path because it lives in the same
repository. A standalone consumer would pin a release instead:

```hcl
module "gke_cluster" {
  source = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/gke-cluster?ref=vX.Y.Z"
  # ...
}
```

(and likewise for `kube-agents-iam`, `chat-pubsub`, and `github-minter`), and
would install the chart from the OCI registry rather than a local path — see
the [chart README](../../../charts/kube-agents/README.md).

## Teardown

`terraform destroy` removes the Helm release, but that also removes the
operator — and the `PlatformAgent` CR carries a finalizer only the operator
can clear, so destroying first strands the CR and hangs the namespace
deletion. Delete the CR and wait for it to disappear **before** destroying:

```bash
kubectl delete platformagent platform-agent -n kubeagents-system --wait
terraform destroy
```

The cluster is created with `deletion_protection = true` by default; set the
variable to `false` (and apply) before a destroy can remove the cluster.
