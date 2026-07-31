variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "cluster_name" {
  description = "GKE Autopilot cluster name"
  type        = string
}

variable "location" {
  description = "GCP location (region or zone)"
  type        = string
}

variable "deletion_protection" {
  description = "Whether deletion protection is enabled on the cluster"
  type        = bool
  default     = true
}
