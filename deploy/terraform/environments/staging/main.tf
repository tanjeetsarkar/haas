# Example staging root module. Cloud-agnostic: bring your own cluster (EKS/GKE/AKS/
# on-prem), managed PostgreSQL, Redis and S3-compatible object store, then pass their
# endpoints here. Credentials should come from TF_VAR_* or a secrets backend, never
# from committed tfvars.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.25.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = ">= 2.12.0, < 3.0.0"
    }
  }

  # Configure an encrypted remote backend (S3+DynamoDB, GCS, azurerm, TFC) per environment.
  # backend "s3" {}
}

provider "kubernetes" {
  config_path    = var.kubeconfig_path
  config_context = var.kube_context
}

provider "helm" {
  kubernetes {
    config_path    = var.kubeconfig_path
    config_context = var.kube_context
  }
}

module "haas" {
  source = "../../modules/haas-platform"

  namespace        = "haas-staging"
  release_name     = "haas"
  chart_path       = "${path.module}/../../../helm/haas"
  image_repository = var.image_repository
  image_digest     = var.image_digest
  image_tag        = var.image_tag
  keda_enabled     = var.keda_enabled
  create_secrets   = var.create_secrets

  postgres     = var.postgres
  redis        = var.redis
  object_store = var.object_store

  oidc                  = var.oidc
  secret_encryption_key = var.secret_encryption_key
  envelope_signing_key  = var.envelope_signing_key
  envelope_verify_key   = var.envelope_verify_key
  openrouter_api_key    = var.openrouter_api_key

  api_hostname              = var.api_hostname
  tool_worker_runtime_class = var.tool_worker_runtime_class
  values_files              = [file("${path.module}/../../../helm/haas/values-staging.yaml")]
}

output "api_service" {
  value = module.haas.api_service
}
