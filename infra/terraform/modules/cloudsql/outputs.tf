output "instance_name" {
  value = google_sql_database_instance.postgres.name
}

output "instance_connection_name" {
  value = google_sql_database_instance.postgres.connection_name
}

output "private_ip_address" {
  value = google_sql_database_instance.postgres.private_ip_address
}

output "db_name" {
  value = google_sql_database.app_db.name
}

output "secret_id" {
  value = google_secret_manager_secret.db_password.secret_id
}

output "public_ip_address" {
  value = google_sql_database_instance.postgres.public_ip_address
}
