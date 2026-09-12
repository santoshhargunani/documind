# main.tf
# -------
# Creates the GCP service account the DocuMind pod will run as, grants
# it least-privilege IAM permissions, and establishes the Workload
# Identity binding to a Kubernetes service account.

resource "google_service_account" "app" {
  project      = var.project_id
  account_id   = var.gsa_account_id
  display_name = "DocuMind application service account"
  description  = "Identity used by the DocuMind pod running in GKE, via Workload Identity"
}
# This is a GCP-native identity — completely separate from any human
# user's identity (like your own gcloud login used for local dev
# throughout this project). It exists solely for this application to
# act as, with its own independently grantable/revokable permissions.

resource "google_project_iam_member" "secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.app.email}"
}
# Grants ONLY the ability to READ secret values — not create, list,
# or delete secrets. This is the exact least-privilege permission
# flagged as the correct scope back in Step 4.4's interview-question
# discussion about config_secrets.py: the app needs to fetch the DB
# password, nothing more.

resource "google_project_iam_member" "vertex_ai_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.app.email}"
}
# Grants the ability to call Vertex AI's prediction/generation APIs
# (Gemini, embeddings) — required for every agent node (router,
# retriever, synthesis, critic) and the ingestion embedder.

resource "google_project_iam_member" "cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.app.email}"
}
# Grants permission to connect to Cloud SQL instances via the Cloud
# SQL Auth Proxy / connector mechanism. Note: our current db.py
# connects via a direct pg8000 TCP connection using host/port/
# password rather than the Cloud SQL connector library — this role is
# still worth granting now since it's the standard, expected
# permission for any workload talking to Cloud SQL, and positions us
# to adopt the connector library later (a more robust connection
# method than a bare IP, discussed as a possible improvement).

resource "google_service_account_iam_member" "workload_identity_binding" {
  service_account_id = google_service_account.app.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.k8s_namespace}/${var.k8s_service_account_name}]"
}
# THIS is the actual Workload Identity bridge — the single most
# important resource in this file. Unlike the three roles above
# (which grant the GSA permission to call GCP APIs), this resource
# grants a SPECIFIC Kubernetes service account (identified by the
# special member string format "serviceAccount:PROJECT_ID.svc.id.goog
# [NAMESPACE/KSA_NAME]") permission to IMPERSONATE this GSA.
#
# In plain terms: this says "any pod running as the Kubernetes
# service account named documind-app, in the default namespace, is
# allowed to act as this GCP service account." The Kubernetes service
# account itself gets created in Step 8.4's manifests — this
# Terraform resource references it by name/namespace before it exists
# yet, which is fine, since IAM bindings don't require the bound
# principal to exist at bind-time for a Kubernetes-external identity
# like this.
