# SOP: Release Validation (Pre-Release Operational Playbook)

**Purpose:** Validates that before a proposed SemVer release tag (`vMAJOR.MINOR.PATCH`) is promoted, all required container images exist in GHCR, OCI Helm charts pass linting, and Terraform modules are syntactically valid.

---

## Execution Checklist

### 1. Container Image Verification

- Verify that prebuilt container images (`platform-agent`, `credential-proxy`, `replay-proxy`, `k8s-operator`) exist in GHCR for the target candidate SHA using read-only tools or `scripts/release/verify_candidate_images.sh`.

### 2. OCI Helm Chart Linting & Validation

- Execute Helm linting against canonical chart directory:
  ```bash
  helm lint charts/kube-agents
  ```
- Verify template rendering with explicit SemVer version overrides.

### 3. Terraform Module Syntax & Constraint Check

- Execute formatting and validation checks across all reusable modules:
  ```bash
  terraform fmt -check -recursive terraform/modules/
  terraform -chdir=terraform/modules/gke-cluster init -backend=false
  terraform -chdir=terraform/modules/gke-cluster validate
  ```
