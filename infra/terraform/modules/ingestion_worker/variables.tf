variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "project_number" {
  type        = string
  description = "GCP project number (used to build Pub/Sub / GCS service agent identities)"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "raw_docs_bucket_name" {
  type        = string
  description = "Bucket whose uploads/deletes trigger ingestion"
}

variable "image" {
  type        = string
  description = "Worker container image to use when Terraform creates the Cloud Run service. CI deploys versioned images afterwards."
}

variable "network_name" {
  type        = string
  description = "VPC network the worker attaches to (to reach Cloud SQL's private IP)"
}

variable "subnet_name" {
  type        = string
  description = "Subnet the worker attaches to"
}

variable "app_db_password_secret_id" {
  type        = string
  description = "Secret Manager secret id holding the documind_app password"
}

variable "env" {
  type        = map(string)
  description = "Plain environment variables for the worker (see documind.config.Settings)"
}

variable "max_instances" {
  type        = number
  default     = 1
  description = "Caps parallel ingestion to protect Vertex AI / Document AI quota and DB connections"
}
