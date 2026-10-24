# =============================================================================
# modules/cloudsql/main.tf
# PLACE AT: infra/terraform/modules/cloudsql/main.tf   (REPLACES the existing file)
#
# WHAT CHANGED: added a second password + secret for the low-privilege
# application role "documind_app" (see the block at the bottom). Everything
# above it is exactly as it was.
# =============================================================================

# Random 24-char password for the ADMIN database user.
resource "random_password" "db_password" {
  length           = 24
  special          = true
  override_special = "!#$%&*()-_=+"
}

# The Cloud SQL Postgres instance (public IP only while authorized_dev_ip is set).
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

# The application database inside the instance.
resource "google_sql_database" "app_db" {
  name     = var.db_name
  project  = var.project_id
  instance = google_sql_database_instance.postgres.name
}

# ADMIN user (humans / migrations only).
resource "google_sql_user" "admin" {
  name     = var.db_admin_user
  project  = var.project_id
  instance = google_sql_database_instance.postgres.name
  password = random_password.db_password.result
}

# Secret Manager entry holding the admin password.
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

# -----------------------------------------------------------------------------
# NEW: password + secret for the low-privilege APP role "documind_app".
#
# The ROLE itself is created by infra/sql/schema.sql (a plain role with no
# special attributes, so Row-Level Security applies to it). We deliberately do
# NOT create it with google_sql_user: users created through the Cloud SQL API
# are placed in the powerful "cloudsqlsuperuser" group, which we don't want the
# app to belong to. Terraform only generates the password and stores it; the
# file infra/sql/set_app_password.sql applies it to the role.
# -----------------------------------------------------------------------------

# Random password for the app role. special = false keeps it alphanumeric so it
# is safe in shell commands and connection strings; 32 chars is still very strong.
resource "random_password" "app_db_password" {
  length  = 32
  special = false
}

# Secret Manager entry -> "documind-postgres-app-password" (instance name + suffix).
resource "google_secret_manager_secret" "app_db_password" {
  secret_id = "${var.instance_name}-app-password"
  project   = var.project_id

  replication {
    auto {}
  }
}

# The actual secret value (version) the application reads at runtime.
resource "google_secret_manager_secret_version" "app_db_password" {
  secret      = google_secret_manager_secret.app_db_password.id
  secret_data = random_password.app_db_password.result
}
