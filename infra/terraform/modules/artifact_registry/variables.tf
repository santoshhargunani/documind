variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "region" {
  type        = string
  description = "GCP region for the Artifact Registry repository"
  default     = "us-central1"
}

variable "repository_id" {
  type        = string
  description = "Name of the Artifact Registry repository"
  default     = "documind"
}
