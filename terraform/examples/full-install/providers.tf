terraform {
  required_version = "~> 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30, < 8.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 5.30, < 8.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = ">= 2.12, < 4.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.25, < 3.0"
    }
  }
}

provider "google" {
  project = var.project_id
}

# The chat-pubsub module's service-identity resources (Workspace Add-ons and
# the Chat API) require the google-beta provider; it inherits this default
# configuration.
provider "google-beta" {
  project = var.project_id
}

# Both in-cluster providers authenticate against the cluster this
# configuration itself creates, using the caller's ADC token.
data "google_client_config" "default" {}

provider "kubernetes" {
  host                   = "https://${module.gke_cluster.cluster_endpoint}"
  token                  = data.google_client_config.default.access_token
  cluster_ca_certificate = base64decode(module.gke_cluster.cluster_ca_certificate)
}

provider "helm" {
  kubernetes = {
    host                   = "https://${module.gke_cluster.cluster_endpoint}"
    token                  = data.google_client_config.default.access_token
    cluster_ca_certificate = base64decode(module.gke_cluster.cluster_ca_certificate)
  }
}
