resource "google_container_cluster" "autopilot" {
  name     = var.cluster_name
  project  = var.project_id
  location = var.region

  enable_autopilot = true
  # Autopilot mode means GCP manages node provisioning, sizing, and
  # scaling entirely — we only ever think in terms of pods and their
  # resource requests, never nodes/VMs directly. This is deliberately
  # simpler than GKE Standard mode (which requires you to manage node
  # pools yourself) and matches what our original project plan called
  # for (GKE Autopilot specifically, per Step 3's infra design).

  network    = var.network_name
  subnetwork = var.subnet_name
  # Deploys into the SAME VPC and subnet we built all the way back in
  # Step 2 — this is what finally makes the private-IP path to Cloud
  # SQL usable: pods in this cluster are genuinely inside the VPC,
  # unlike your laptop.

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }
  # THIS is the line that creates the identity pool our
  # workload_identity module's binding resource was missing —
  # explicitly enabling Workload Identity on the cluster itself
  # (technically on by default for Autopilot as of recent GKE
  # versions, but declaring it explicitly rather than relying on an
  # implicit default is the more defensible, self-documenting choice
  # for infrastructure code).

  deletion_protection = false
  # Same reasoning as Cloud SQL's deletion_protection setting back in
  # Step 3 — set to false deliberately so terraform destroy can
  # actually tear this down between sessions. A real production
  # cluster would set this true.
}

resource "google_project_iam_member" "gke_node_artifact_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${var.project_number}-compute@developer.gserviceaccount.com"
}
