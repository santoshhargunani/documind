variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "github_repo" {
  type        = string
  description = "GitHub repo in 'owner/repo' format allowed to authenticate"
}

variable "ci_sa_account_id" {
  type    = string
  default = "documind-ci"
}
