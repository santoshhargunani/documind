output "dataset_id" {
  value = google_bigquery_dataset.eval.dataset_id
}

output "table_id" {
  value = google_bigquery_table.eval_results.table_id
}
