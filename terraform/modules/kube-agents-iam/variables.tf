variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "service_account_id" {
  description = "IAM Service Account ID for Kube-Agents"
  type        = string
  default     = "kubeagents-platform-gsa"
}

variable "namespace" {
  description = "Kubernetes namespace where Kube-Agents runs"
  type        = string
  default     = "kubeagents-system"
}

variable "ksa_name" {
  description = "Kubernetes Service Account name"
  type        = string
  default     = "kubeagents-platform-agent"
}
