resource "google_storage_bucket" "raw_docs" {
  name                        = var.raw_docs_bucket_name
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true

  versioning {
    enabled = false
  }

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_document_ai_processor" "ocr_parser" {
  project      = var.project_id
  location     = "us"
  display_name = var.processor_display_name
  type         = "OCR_PROCESSOR"
}

