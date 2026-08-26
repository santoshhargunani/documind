output "bucket_name" {
  value = google_storage_bucket.raw_docs.name
}

output "processor_id" {
  value = google_document_ai_processor.ocr_parser.name
}
