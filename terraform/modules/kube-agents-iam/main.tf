resource "google_service_account" "agent" {
  project      = var.project_id
  account_id   = var.service_account_id
  display_name = "Kube-Agents Platform Agent Service Account"
}

resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.agent.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${var.ksa_name}]"
}

resource "google_project_iam_member" "agent_roles" {
  #checkov:skip=CKV_GCP_41:Platform agent requires serviceAccountUser role to manage agent workload identities
  #checkov:skip=CKV_GCP_49:Platform agent requires serviceAccountUser role to manage agent workload identities
  for_each = toset(var.project_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.agent.email}"
}
