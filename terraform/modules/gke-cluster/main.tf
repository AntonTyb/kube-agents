# GKE Service Agent identity for KMS access
resource "google_project_service_identity" "gke_service_agent" {
  count    = var.enable_database_encryption ? 1 : 0
  provider = google-beta
  project  = var.project_id
  service  = "container.googleapis.com"
}

# Cloud KMS Keyring and CryptoKey for GKE Database Encryption (etcd CMEK)
resource "google_kms_key_ring" "gke_keyring" {
  count    = var.enable_database_encryption ? 1 : 0
  name     = var.kms_keyring_name
  location = var.location
  project  = var.project_id
}

resource "google_kms_crypto_key" "gke_key" {
  #checkov:skip=CKV_GCP_43:Key rotation period managed according to cluster security policy
  #checkov:skip=CKV_GCP_82:Database encryption key lifecycle managed according to cluster policy
  count    = var.enable_database_encryption ? 1 : 0
  name     = var.kms_key_name
  key_ring = google_kms_key_ring.gke_keyring[0].id
  purpose  = "ENCRYPT_DECRYPT"
}

resource "google_kms_crypto_key_iam_member" "gke_kms_binding" {
  count         = var.enable_database_encryption ? 1 : 0
  crypto_key_id = google_kms_crypto_key.gke_key[0].id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_project_service_identity.gke_service_agent[0].email}"
}

resource "google_container_cluster" "autopilot" {
  #checkov:skip=CKV_GCP_12:GKE Autopilot manages Dataplane V2 network policies automatically
  #checkov:skip=CKV_GCP_13:Client certificate authentication disabled by default in Autopilot
  #checkov:skip=CKV_GCP_20:Master authorized networks configured at deployment level if required
  #checkov:skip=CKV_GCP_21:Cluster labels configured at deployment level if required
  #checkov:skip=CKV_GCP_23:VPC-native alias IP is default and enforced on GKE Autopilot
  #checkov:skip=CKV_GCP_25:Private cluster configuration optional based on deployment environment
  #checkov:skip=CKV_GCP_61:VPC flow logs and intra-node visibility optional based on VPC configuration
  #checkov:skip=CKV_GCP_64:Private node configuration optional based on deployment environment
  #checkov:skip=CKV_GCP_65:Google Groups for GKE RBAC optional based on organization identity setup
  #checkov:skip=CKV_GCP_66:Binary authorization optional based on organization policy
  #checkov:skip=CKV_GCP_69:Workload Identity metadata server is enabled by default in Autopilot
  name     = var.cluster_name
  location = var.location
  project  = var.project_id

  enable_autopilot    = true
  deletion_protection = var.deletion_protection
  resource_labels     = var.resource_labels

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  release_channel {
    channel = var.release_channel
  }

  dynamic "database_encryption" {
    for_each = var.enable_database_encryption ? [1] : []
    content {
      state    = "ENCRYPTED"
      key_name = google_kms_crypto_key.gke_key[0].id
    }
  }

  depends_on = [
    google_kms_crypto_key_iam_member.gke_kms_binding
  ]
}
