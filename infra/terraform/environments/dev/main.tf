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
