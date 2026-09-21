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
