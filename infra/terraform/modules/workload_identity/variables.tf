# variables.tf
# ------------
# Inputs for the workload_identity module: creates a dedicated GCP
# service account for the DocuMind pod and grants it exactly the
# permissions it needs — nothing more.

variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "gsa_account_id" {
  type        = string
  description = "Account ID (short name) for the GCP service account, e.g. 'documind-app'"
  default     = "documind-app"
}

variable "k8s_namespace" {
  type        = string
  description = "Kubernetes namespace the pod will run in"
  default     = "default"
}

variable "k8s_service_account_name" {
  type        = string
  description = "Name of the Kubernetes service account that will be bound to this GCP service account"
  default     = "documind-app"
}
