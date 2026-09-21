variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "region" {
  type        = string
  description = "GCP region for the BigQuery dataset"
  default     = "us-central1"
}

variable "dataset_id" {
  type        = string
  description = "BigQuery dataset name for DocuMind eval results"
  default     = "documind_eval"
}
