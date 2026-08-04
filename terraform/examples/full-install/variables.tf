variable "project_id" {
  description = "GCP Project ID everything is provisioned in"
  type        = string
}

variable "cluster_name" {
  description = "Name of the GKE Autopilot cluster to create"
  type        = string
}

variable "location" {
  description = "GCP region for the cluster (and the KMS key ring when the GitHub minter is enabled). Autopilot clusters are regional, so a zone is rejected by the gke-cluster module."
  type        = string
}

variable "deletion_protection" {
  description = "Whether deletion protection is enabled on the cluster. Passed through to the gke-cluster module; must be false before `terraform destroy` can remove the cluster."
  type        = bool
  default     = true
}

variable "release_channel" {
  description = "GKE release channel for the cluster (RAPID, REGULAR, STABLE, or EXTENDED)"
  type        = string
  default     = "REGULAR"
}

variable "namespace" {
  description = "Kubernetes namespace the kube-agents release is installed into and the Workload Identity binding targets"
  type        = string
  default     = "kubeagents-system"
}

variable "project_roles" {
  description = "Project-level IAM roles granted to the agent's service account. Leave null to use the kube-agents-iam module's default read-only permission set; set to [] to grant nothing and manage roles externally."
  type        = list(string)
  default     = null
}

variable "image_tag" {
  description = "Image tag for both the operator and the platform agent. Required because a checkout's Chart.yaml carries an appVersion placeholder that never matches a published image tag, so the chart's tag defaulting cannot work from a checkout. `latest` is fine for evaluation; set a `vX.Y.Z` release tag for production."
  type        = string
  default     = "latest"
}

variable "api_server_key" {
  description = "API_SERVER_KEY for the agent harness (required; stored in the platform-agent-secrets Secret)"
  type        = string
  sensitive   = true
}

variable "anthropic_api_key" {
  description = "ANTHROPIC_API_KEY model-provider credential (optional; omitted from the Secret when empty)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "gemini_api_key" {
  description = "GEMINI_API_KEY model-provider credential (optional; omitted from the Secret when empty)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "openai_api_key" {
  description = "OPENAI_API_KEY model-provider credential (optional; omitted from the Secret when empty)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "enable_google_chat" {
  description = "Provision the Google Chat backend (Pub/Sub topic and subscription, Chat APIs). See the README: the chart does not yet wire the CR's googleChat section, so the CR must be patched manually."
  type        = bool
  default     = false
}

variable "enable_github_minter" {
  description = "Provision the GitHub token minter's GCP resources (service account, KMS key ring and signing key)"
  type        = bool
  default     = false
}
