resource "random_password" "db_password" {
  length           = 24
  special          = true
  override_special = "!#$%&*()-_=+"
}

resource "google_sql_database_instance" "postgres" {
  name                = var.instance_name
  project             = var.project_id
  region              = var.region
  database_version    = "POSTGRES_15"
  deletion_protection = false

  settings {
    tier = var.tier

    ip_configuration {
      ipv4_enabled    = var.authorized_dev_ip != "" ? true : false
      private_network = var.network_id
      ssl_mode        = "ENCRYPTED_ONLY"

      dynamic "authorized_networks" {
        for_each = var.authorized_dev_ip != "" ? [var.authorized_dev_ip] : []
        content {
          name  = "dev-laptop"
          value = authorized_networks.value
        }
      }
    }

    backup_configuration {
      enabled = true
    }
  }
}

resource "google_sql_database" "app_db" {
  name     = var.db_name
  project  = var.project_id
  instance = google_sql_database_instance.postgres.name
}

resource "google_sql_user" "admin" {
  name     = var.db_admin_user
  project  = var.project_id
  instance = google_sql_database_instance.postgres.name
  password = random_password.db_password.result
}

resource "google_secret_manager_secret" "db_password" {
  secret_id = "${var.instance_name}-password"
  project   = var.project_id

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = random_password.db_password.result
}
