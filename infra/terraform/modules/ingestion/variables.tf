variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "region" {
  type        = string
  description = "GCP region"
  default     = "us-central1"
}

variable "raw_docs_bucket_name" {
  type        = string
  description = "Cloud Storage bucket for raw uploaded documents"
}

variable "processor_display_name" {
  type        = string
  description = "Display name for the Document AI processor"
  default     = "documind-ocr-parser"
}
