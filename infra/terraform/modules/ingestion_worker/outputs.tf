output "worker_url" {
  value = google_cloud_run_v2_service.worker.uri
}

output "worker_service_name" {
  value = google_cloud_run_v2_service.worker.name
}

output "worker_sa_email" {
  value = google_service_account.worker.email
}

output "dead_letter_subscription" {
  value = google_pubsub_subscription.dead_letter_hold.name
}
