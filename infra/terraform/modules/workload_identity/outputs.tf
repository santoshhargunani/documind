# outputs.tf
# ----------
# Values other parts of the Terraform config (and the Kubernetes
# manifests in Step 8.4) will need to reference.

output "gsa_email" {
  value = google_service_account.app.email
}
# The full GCP service account email (e.g.
# documind-app@project-id.iam.gserviceaccount.com) — this exact value
# needs to be added as an ANNOTATION on the Kubernetes service account
# in Step 8.4's manifests, which is the other half of completing the
# Workload Identity link (the Terraform binding above grants
# permission; the K8s-side annotation is what actually activates the
# connection for a running pod).
