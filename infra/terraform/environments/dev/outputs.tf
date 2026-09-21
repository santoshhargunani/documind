output "instance_connection_name" {
  value = module.cloudsql.instance_connection_name
}

output "private_ip_address" {
  value = module.cloudsql.private_ip_address
}

output "db_name" {
  value = module.cloudsql.db_name
}

output "secret_id" {
  value = module.cloudsql.secret_id
}

output "network_id" {
  value = module.vpc.network_id
}

output "raw_docs_bucket" {
  value = module.ingestion.bucket_name
}

output "docai_processor_id" {
  value = module.ingestion.processor_id
}

output "public_ip_address" {
  value = module.cloudsql.public_ip_address
}

output "artifact_registry_url" {
  value = module.artifact_registry.repository_url
}

output "gsa_email" {
  value = module.workload_identity.gsa_email
}

output "gke_cluster_name" {
  value = module.gke.cluster_name
}

output "bq_dataset_id" {
  value = module.bigquery.dataset_id
}
