resource "google_bigquery_dataset" "eval" {
  project    = var.project_id
  dataset_id = var.dataset_id
  location   = var.region

  delete_contents_on_destroy = true
  # Same deliberate choice as Cloud SQL's deletion_protection = false
  # — lets terraform destroy actually tear this down between
  # sessions, consistent with this whole project's cost-control
  # workflow. A real production dataset holding real historical eval
  # data would set this false to prevent accidental data loss.
}

resource "google_bigquery_table" "eval_results" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.eval.dataset_id
  table_id   = "eval_results"

  deletion_protection = false

  schema = jsonencode([
    { name = "run_id", type = "STRING", mode = "REQUIRED" },
    { name = "run_timestamp", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "question", type = "STRING", mode = "REQUIRED" },
    { name = "passed", type = "BOOLEAN", mode = "REQUIRED" },
    { name = "is_grounded", type = "BOOLEAN", mode = "NULLABLE" },
    { name = "retry_count", type = "INTEGER", mode = "REQUIRED" },
    { name = "latency_seconds", type = "FLOAT", mode = "REQUIRED" },
    { name = "final_answer", type = "STRING", mode = "NULLABLE" },
    { name = "missing_keywords", type = "STRING", mode = "REPEATED" },
  ])
  # An explicit schema, defined in code, rather than relying on
  # BigQuery's schema auto-detection from the first insert — this is
  # the same "infrastructure as code, not click-ops" principle
  # applied throughout this project. `run_id` (shared across every
  # row from the same eval run) is what lets you GROUP BY run later
  # to compute per-run aggregates; `missing_keywords` as REPEATED
  # (BigQuery's term for an array column) stores the list directly
  # rather than needing a separate join table.
}
