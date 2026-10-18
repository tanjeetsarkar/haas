# Offline tests (no cluster): `terraform init -backend=false && terraform test`.
mock_provider "kubernetes" {}
mock_provider "helm" {}

variables {
  image_repository = "registry.example.com/haas/haas-distributed"
  image_digest     = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
  postgres = {
    api_dsn       = "postgresql://haas_api:x@db:5432/haas"
    executor_dsn  = "postgresql://haas_executor:x@db:5432/haas"
    migration_dsn = "postgresql://owner:x@db:5432/haas"
    cidrs         = ["10.20.0.0/16"]
  }
  redis = {
    url                = "rediss://:x@redis:6379/0"
    broker_url         = "rediss://:x@redis:6379/1"
    result_backend_url = "rediss://:x@redis:6379/2"
    address            = "redis:6379"
    password           = "x"
    cidrs              = ["10.30.0.0/16"]
  }
}

run "external_secrets_by_default" {
  command = plan

  assert {
    condition     = length(kubernetes_secret_v1.haas) == 0
    error_message = "No Secrets should be created unless create_secrets = true."
  }

  assert {
    condition     = kubernetes_namespace_v1.haas[0].metadata[0].labels["pod-security.kubernetes.io/enforce"] == "restricted"
    error_message = "Namespace must enforce Pod Security 'restricted'."
  }

  assert {
    condition     = helm_release.haas.atomic
    error_message = "Release must be atomic (auto-rollback on failed upgrade)."
  }
}

run "managed_secrets_least_privilege" {
  command = plan

  variables {
    create_secrets        = true
    keda_enabled          = true
    secret_encryption_key = "k"
    envelope_signing_key  = "s"
    envelope_verify_key   = "v"
    openrouter_api_key    = "o"
    oidc = {
      issuer   = "https://issuer.example.com"
      audience = "haas"
      jwks_url = "https://issuer.example.com/jwks"
    }
    object_store = {
      endpoint = "https://s3.example.com"
      bucket   = "haas-artifacts"
    }
  }

  assert {
    condition = toset(keys(kubernetes_secret_v1.haas)) == toset([
      "haas-api-db", "haas-crypto", "haas-envelope-signing", "haas-envelope-verify",
      "haas-executor-db", "haas-migration-db", "haas-model-provider", "haas-object-store",
      "haas-oidc", "haas-redis", "haas-redis-auth",
    ])
    error_message = "Unexpected managed Secret set."
  }

  assert {
    condition     = yamldecode(helm_release.haas.values[0]).keda.redis.passwordSecret == "haas-redis-auth"
    error_message = "KEDA must authenticate to Redis when a password is set."
  }

  assert {
    condition     = !contains(keys(yamldecode(helm_release.haas.values[0]).toolWorker), "secretEnv")
    error_message = "Tool workers must not receive object-store credentials."
  }
}

run "rejects_mutable_digest" {
  command = plan

  variables {
    image_digest = "latest"
  }

  expect_failures = [var.image_digest]
}
