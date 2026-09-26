resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "github-actions-pool"
  display_name              = "GitHub Actions"
}
# A "pool" is the container for external identities GCP will trust.
# This is the WIF equivalent of the .svc.id.goog identity pool GKE
# auto-created in Step 8.3 — same underlying mechanism (federating an
# external identity provider into GCP IAM), just federating GitHub's
# OIDC tokens instead of Kubernetes service accounts.

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-provider"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }
  # Maps claims from GitHub's OIDC token into attributes GCP's IAM can
  # reference. "attribute.repository" is what lets the binding below
  # restrict trust to ONE specific repo, not any GitHub Actions run
  # anywhere.
  attribute_condition = "assertion.repository == \"${var.github_repo}\""
  # Required by GCP's current WIF security policy — the pool provider
  # itself must explicitly restrict which external identities it will
  # accept, evaluated at token-exchange time, in addition to (not
  # instead of) the IAM binding's attribute.repository restriction on
  # who can actually impersonate the service account. Two independent
  # layers checking the same constraint — deliberate defense in
  # depth, not redundant.
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "ci" {
  project      = var.project_id
  account_id   = var.ci_sa_account_id
  display_name = "DocuMind CI/CD service account"
}

resource "google_service_account_iam_member" "wif_binding" {
  service_account_id = google_service_account.ci.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repo}"
  # This is the actual trust restriction — ONLY workflow runs coming
  # from this exact "owner/repo" can impersonate this service account.
  # Same "least privilege, tightly scoped" principle as every other
  # IAM binding in this project.
}

resource "google_project_iam_member" "ci_artifact_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.ci.email}"
}

resource "google_project_iam_member" "ci_gke_deployer" {
  project = var.project_id
  role    = "roles/container.developer"
  member  = "serviceAccount:${google_service_account.ci.email}"
}

resource "google_project_iam_member" "ci_vertex_ai" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.ci.email}"
  # Needed so the eval-gate job can actually call Gemini/embeddings.
}

resource "google_project_iam_member" "ci_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.ci.email}"
  # Needed for the eval-gate job to fetch the DB password, same as
  # documind-app's runtime permission.
}
