variable "project_id" {
  type        = string
  description = "GCP project ID where resources will be created"
}

variable "region" {
  type        = string
  description = "GCP region for regional resources"
  default     = "us-central1"
}

variable "network_name" {
  type        = string
  description = "Name for the VPC network"
  default     = "documind-vpc"
}

variable "subnet_cidr" { // Ip's for VM or servers
  type        = string
  description = "Primary CIDR range for the GKE subnet"
  default     = "10.0.0.0/20"
}

variable "pods_cidr" { // ip's for pods/containers
  type        = string
  description = "Secondary CIDR range for GKE pod IPs"
  default     = "10.4.0.0/14"
}

variable "services_cidr" {
  type        = string
  description = "Secondary CIDR range for GKE service IPs"
  default     = "10.8.0.0/20"
}
