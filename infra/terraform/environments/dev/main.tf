module "vpc" {
  source = "../../modules/vpc"

  project_id = var.project_id
  region     = var.region
}

module "cloudsql" {
  source = "../../modules/cloudsql"

  project_id        = var.project_id
  region            = var.region
  network_id        = module.vpc.network_id
  authorized_dev_ip = var.authorized_dev_ip

  depends_on = [module.vpc]
}

module "ingestion" {
  source = "../../modules/ingestion"

  project_id           = var.project_id
  region               = var.region
  raw_docs_bucket_name = "${var.project_id}-documind-raw-docs"
}

module "ingestion_worker" {
  source = "../../modules/ingestion_worker"

  project_id     = var.project_id
  project_number = "316104585600"
  region         = var.region

  raw_docs_bucket_name      = module.ingestion.bucket_name
  image                     = "${module.artifact_registry.repository_url}/documind-worker:latest"
  network_name              = module.vpc.network_name
  subnet_name               = module.vpc.subnet_name
  app_db_password_secret_id = "documind-postgres-app-password"

  env = {
    GCP_PROJECT_ID              = var.project_id
    GCP_REGION                  = var.region
    RAW_DOCS_BUCKET             = module.ingestion.bucket_name
    DOCAI_LOCATION              = "us"
    DOCAI_PROCESSOR_ID          = element(split("/", module.ingestion.processor_id), length(split("/", module.ingestion.processor_id)) - 1)
    DB_INSTANCE_CONNECTION_NAME = module.cloudsql.instance_connection_name
    DB_NAME                     = module.cloudsql.db_name
    DB_HOST                     = module.cloudsql.private_ip_address
    DB_PASSWORD_SECRET_ID       = module.cloudsql.secret_id
    DB_SSLMODE                  = "require"
  }

  depends_on = [module.ingestion, module.cloudsql]
}

module "artifact_registry" {
  source = "../../modules/artifact_registry"

  project_id = var.project_id
  region     = var.region
}

module "workload_identity" {
  source = "../../modules/workload_identity"

  project_id = var.project_id
}

module "gke" {
  source = "../../modules/gke"

  project_id   = var.project_id
  region       = var.region
  network_name = module.vpc.network_name
  subnet_name  = module.vpc.subnet_name

  project_number = "316104585600"
  depends_on     = [module.vpc]
}

module "bigquery" {
  source = "../../modules/bigquery"

  project_id = var.project_id
  region     = var.region
}

module "github_ci" {
  source = "../../modules/github_ci"

  project_id  = var.project_id
  github_repo = "your-github-username/documind"
}
