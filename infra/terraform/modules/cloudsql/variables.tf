variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "region" {
  type        = string
  description = "GCP region for the Cloud SQL instance"
  default     = "us-central1"
}

variable "network_id" {
  type        = string
  description = "VPC network ID this instance connects to privately (from the vpc module)"
}

variable "instance_name" {
  type        = string
  description = "Name for the Cloud SQL instance"
  default     = "documind-postgres"
}

variable "tier" {
  type        = string
  description = "Machine tier. db-f1-micro is the cheapest (shared vCPU, 0.6GB RAM) — fine for a prep project, not for real production load"
  default     = "db-f1-micro"
}

variable "db_name" {
  type        = string
  description = "Name of the application database"
  default     = "documind"
}

variable "db_admin_user" {
  type        = string
  description = "Database admin username"
  default     = "documind_admin"
}

variable "authorized_dev_ip" {
  type        = string
  description = "Your local IP (CIDR, e.g. 1.2.3.4/32) allowed to connect for local development. Leave empty to disable public access."
  default     = ""
}
