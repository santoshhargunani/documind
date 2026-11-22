# =============================================================================
# Event-driven ingestion:
#   GCS object finalize/delete -> Pub/Sub topic -> push subscription (OIDC)
#     -> private Cloud Run worker (documind.worker.app) -> ingest_object()
#   Failed deliveries retry with backoff, then land in a dead-letter topic.
# =============================================================================

locals {
  gcs_service_agent    = "serviceAccount:${data.google_storage_project_service_account.gcs.email_address}"
  pubsub_service_agent = "serviceAccount:service-${var.project_number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# Looking this up also provisions the GCS service agent in fresh projects.
data "google_storage_project_service_account" "gcs" {
  project = var.project_id
}

resource "google_project_service" "apis" {
  for_each = toset(["run.googleapis.com", "pubsub.googleapis.com"])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# ---- Identities -------------------------------------------------------------

# Runtime identity of the worker: least privilege for what ingestion does.
resource "google_service_account" "worker" {
  project      = var.project_id
  account_id   = "documind-ingest-worker"
  display_name = "DocuMind ingestion worker"
}

# Identity Pub/Sub uses to call the (private) worker.
resource "google_service_account" "pubsub_invoker" {
  project      = var.project_id
  account_id   = "documind-pubsub-invoker"
  display_name = "DocuMind Pub/Sub push invoker"
}

resource "google_project_iam_member" "worker_roles" {
  for_each = toset([
    "roles/documentai.apiUser", # call the OCR processor
    "roles/aiplatform.user",    # Vertex AI embeddings
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_storage_bucket_iam_member" "worker_read" {
  bucket = var.raw_docs_bucket_name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_secret_manager_secret_iam_member" "worker_app_password" {
  project   = var.project_id
  secret_id = var.app_db_password_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"
}

# ---- Pub/Sub ----------------------------------------------------------------

resource "google_pubsub_topic" "uploads" {
  project    = var.project_id
  name       = "documind-raw-docs-events"
  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic" "dead_letter" {
  project    = var.project_id
  name       = "documind-raw-docs-events-dlq"
  depends_on = [google_project_service.apis]
}

# Pull subscription so dead-lettered messages are retained and inspectable.
resource "google_pubsub_subscription" "dead_letter_hold" {
  project                    = var.project_id
  name                       = "documind-raw-docs-events-dlq-hold"
  topic                      = google_pubsub_topic.dead_letter.id
  message_retention_duration = "604800s" # 7 days
  ack_deadline_seconds       = 60
}

# The GCS service agent must be allowed to publish to the topic.
resource "google_pubsub_topic_iam_member" "gcs_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.uploads.name
  role    = "roles/pubsub.publisher"
  member  = local.gcs_service_agent
}

resource "google_storage_notification" "raw_docs" {
  bucket         = var.raw_docs_bucket_name
  topic          = google_pubsub_topic.uploads.id
  payload_format = "JSON_API_V1"
  event_types    = ["OBJECT_FINALIZE", "OBJECT_DELETE"]

  depends_on = [google_pubsub_topic_iam_member.gcs_publisher]
}

# ---- Cloud Run worker -------------------------------------------------------

resource "google_cloud_run_v2_service" "worker" {
  project             = var.project_id
  name                = "documind-ingest-worker"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL" # still private: no allUsers invoker is granted
  deletion_protection = false

  template {
    service_account = google_service_account.worker.email
    timeout         = "600s" # fits inside Pub/Sub's max 600s ack deadline

    # One document at a time per instance; scale out instead (quota/memory safety).
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }

    # Direct VPC egress so the worker can reach Cloud SQL's private IP.
    # Only private ranges go through the VPC; Google APIs use the normal path.
    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = var.network_name
        subnetwork = var.subnet_name
      }
    }

    containers {
      image = var.image

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      dynamic "env" {
        for_each = var.env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  lifecycle {
    # CI deploys new images; don't let `terraform apply` roll them back.
    ignore_changes = [template[0].containers[0].image, client, client_version]
  }

  depends_on = [google_project_service.apis]
}

# Only the Pub/Sub invoker identity may call the worker.
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.worker.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.pubsub_invoker.email}"
}

# ---- Push subscription ------------------------------------------------------

# Pub/Sub needs to mint OIDC tokens as the invoker SA ...
resource "google_service_account_iam_member" "pubsub_token_creator" {
  service_account_id = google_service_account.pubsub_invoker.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.pubsub_service_agent
}

# ... and to forward failed messages to the DLQ topic / ack from its subscription.
resource "google_pubsub_topic_iam_member" "dlq_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.dead_letter.name
  role    = "roles/pubsub.publisher"
  member  = local.pubsub_service_agent
}

resource "google_pubsub_subscription" "push" {
  project              = var.project_id
  name                 = "documind-raw-docs-events-push"
  topic                = google_pubsub_topic.uploads.id
  ack_deadline_seconds = 600

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.worker.uri}/pubsub/push"

    oidc_token {
      service_account_email = google_service_account.pubsub_invoker.email
      audience              = google_cloud_run_v2_service.worker.uri
    }
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = 6
  }

  depends_on = [
    google_service_account_iam_member.pubsub_token_creator,
    google_pubsub_topic_iam_member.dlq_publisher,
    google_cloud_run_v2_service_iam_member.invoker,
  ]
}

# Pub/Sub must be able to ack messages on this subscription when dead-lettering.
resource "google_pubsub_subscription_iam_member" "dlq_subscriber" {
  project      = var.project_id
  subscription = google_pubsub_subscription.push.name
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_service_agent
}
