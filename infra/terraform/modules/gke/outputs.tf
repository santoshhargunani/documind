output "cluster_name" {
  value = google_container_cluster.autopilot.name
}

output "cluster_endpoint" {
  value     = google_container_cluster.autopilot.endpoint
  sensitive = true
}
# Marked sensitive because the cluster endpoint combined with
# credentials could be used to access the cluster's control plane —
# Terraform will hide this value in CLI output by default (though it
# still lands in the state file, same caveat as every other sensitive
# value we've handled this project).
