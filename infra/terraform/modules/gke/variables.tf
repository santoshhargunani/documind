variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "region" {
  type        = string
  description = "GCP region for the GKE Autopilot cluster"
  default     = "us-central1"
}

variable "cluster_name" {
  type        = string
  description = "Name for the GKE cluster"
  default     = "documind-cluster"
}

variable "network_name" {
  type        = string
  description = "VPC network name this cluster runs in (from the vpc module)"
}

variable "subnet_name" {
  type        = string
  description = "Subnet name this cluster's nodes/pods use (from the vpc module)"
}
