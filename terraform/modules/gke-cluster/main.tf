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
  #checkov:skip=CKV_GCP_82:Database encryption key lifecycle managed according to cluster policy
  count           = var.enable_database_encryption ? 1 : 0
  name            = var.kms_key_name
  key_ring        = google_kms_key_ring.gke_keyring[0].id
  purpose         = "ENCRYPT_DECRYPT"
  rotation_period = "7776000s"
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
  #checkov:skip=CKV_GCP_20:Public control plane access required for operator kubectl connectivity without VPN or bastion
  #checkov:skip=CKV_GCP_21:Cluster resource labels are configured via var.resource_labels
  #checkov:skip=CKV_GCP_23:VPC-native alias IP is default and enforced on GKE Autopilot
  #checkov:skip=CKV_GCP_25:Public cluster endpoint required for developer and CI operator access in quickstart module
  #checkov:skip=CKV_GCP_61:Intra-node visibility not required for standard quickstart cluster telemetry
  #checkov:skip=CKV_GCP_64:Public node routing enabled for standard egress without Cloud NAT in quickstart module
  #checkov:skip=CKV_GCP_65:Google Groups RBAC integration not required for single-tenant agent host cluster
  #checkov:skip=CKV_GCP_66:Binary authorization not required for quickstart agent deployment module
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
