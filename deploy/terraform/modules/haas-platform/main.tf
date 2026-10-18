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
}

locals {
  namespace = var.create_namespace ? kubernetes_namespace_v1.haas[0].metadata[0].name : var.namespace

  object_store_secret_env = var.object_store == null ? {} : {
    for key, env_name in var.object_store_env_names :
    env_name => { name = "haas-object-store", key = replace(key, "_", "-") }
    if try(nonsensitive(var.object_store[key]), "") != ""
  }

  generated_values = {
    profile = var.profile
    image = {
      repository = var.image_repository
      tag        = var.image_tag
      digest     = var.image_digest
    }
    api = {
      secretEnv = local.object_store_secret_env
      ingress = {
        enabled   = var.api_hostname != ""
        className = var.ingress_class_name
        annotations = {
          "nginx.ingress.kubernetes.io/proxy-buffering"    = "off"
          "nginx.ingress.kubernetes.io/proxy-read-timeout" = "3600"
        }
        hosts = var.api_hostname == "" ? [] : [{ host = var.api_hostname, paths = ["/"] }]
        tls   = var.api_hostname == "" ? [] : [{ secretName = var.tls_secret_name, hosts = [var.api_hostname] }]
      }
    }
    orchestrator = {
      secretEnv = local.object_store_secret_env
    }
    maintenance = {
      secretEnv = local.object_store_secret_env
    }
    toolWorker = {
      runtimeClassName = var.tool_worker_runtime_class
    }
    keda = {
      enabled = var.keda_enabled
      redis = {
        address        = nonsensitive(var.redis.address)
        databaseIndex  = tostring(nonsensitive(var.redis.broker_db_index))
        enableTLS      = tostring(nonsensitive(var.redis.tls))
        passwordSecret = nonsensitive(var.redis.password) == "" ? "" : "haas-redis-auth"
      }
    }
    networkPolicy = {
      enabled = true
      apiIngressFrom = [
        for ns in var.api_ingress_namespaces : {
          namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = ns } }
        }
      ]
      postgres = {
        to    = [for cidr in nonsensitive(var.postgres.cidrs) : { ipBlock = { cidr = cidr } }]
        ports = [{ protocol = "TCP", port = nonsensitive(var.postgres.port) }]
      }
      redis = {
        to    = [for cidr in nonsensitive(var.redis.cidrs) : { ipBlock = { cidr = cidr } }]
        ports = [{ protocol = "TCP", port = nonsensitive(var.redis.port) }]
      }
    }
  }
}

resource "kubernetes_namespace_v1" "haas" {
  count = var.create_namespace ? 1 : 0

  metadata {
    name = var.namespace
    labels = {
      "app.kubernetes.io/part-of"                  = "haas"
      "pod-security.kubernetes.io/enforce"         = "restricted"
      "pod-security.kubernetes.io/enforce-version" = "latest"
      "pod-security.kubernetes.io/audit"           = "restricted"
      "pod-security.kubernetes.io/warn"            = "restricted"
    }
  }
}

# Optional: Secrets referenced by the chart. Prefer External Secrets / Vault in
# production so credentials never enter Terraform state.
locals {
  secrets = var.create_secrets ? merge(
    {
      "haas-redis" = {
        "redis-url"          = var.redis.url
        "broker-url"         = var.redis.broker_url
        "result-backend-url" = var.redis.result_backend_url
      }
      "haas-api-db"           = { "postgres-dsn" = var.postgres.api_dsn }
      "haas-executor-db"      = { "postgres-dsn" = var.postgres.executor_dsn }
      "haas-migration-db"     = { "postgres-dsn" = var.postgres.migration_dsn }
      "haas-crypto"           = { "secret-encryption-key" = var.secret_encryption_key }
      "haas-envelope-signing" = { "signing-key" = var.envelope_signing_key }
      "haas-envelope-verify"  = { "verify-key" = var.envelope_verify_key }
      "haas-model-provider"   = { "openrouter-api-key" = var.openrouter_api_key }
      "haas-oidc" = var.oidc == null ? {} : {
        "issuer"   = var.oidc.issuer
        "audience" = var.oidc.audience
        "jwks-url" = var.oidc.jwks_url
      }
    },
    nonsensitive(var.redis.password) == "" ? {} : { "haas-redis-auth" = { "password" = var.redis.password } },
    var.object_store == null ? {} : {
      "haas-object-store" = {
        for key, value in var.object_store : replace(key, "_", "-") => value if value != ""
      }
    },
  ) : {}
}

resource "kubernetes_secret_v1" "haas" {
  for_each = nonsensitive(toset(keys(local.secrets)))

  metadata {
    name      = each.key
    namespace = local.namespace
    labels = {
      "app.kubernetes.io/part-of"    = "haas"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  data = local.secrets[each.key]
  type = "Opaque"
}

resource "helm_release" "haas" {
  name       = var.release_name
  namespace  = local.namespace
  repository = var.chart_repository == "" ? null : var.chart_repository
  chart      = var.chart_path
  version    = var.chart_version

  atomic          = true
  cleanup_on_fail = true
  wait            = true
  timeout         = 900

  values = concat([yamlencode(local.generated_values)], var.values_files)

  depends_on = [kubernetes_secret_v1.haas]
}
