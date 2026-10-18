variable "namespace" {
  description = "Kubernetes namespace for the release (created with Pod Security 'restricted' labels)."
  type        = string
  default     = "haas"
}

variable "create_namespace" {
  description = "Create the namespace. Set false when it is managed elsewhere (it must still enforce PSA restricted)."
  type        = bool
  default     = true
}

variable "release_name" {
  description = "Helm release name."
  type        = string
  default     = "haas"
}

variable "chart_path" {
  description = "Path to the HaaS Helm chart (or a chart reference when chart_repository is set)."
  type        = string
  default     = "../../helm/haas"
}

variable "chart_repository" {
  description = "Optional Helm repository URL. Empty means chart_path is a local directory."
  type        = string
  default     = ""
}

variable "chart_version" {
  description = "Chart version when installing from a repository."
  type        = string
  default     = null
}

variable "image_repository" {
  description = "Container image repository for haas-distributed."
  type        = string
}

variable "image_digest" {
  description = "Immutable image digest (sha256:...). Preferred over image_tag."
  type        = string
  default     = ""

  validation {
    condition     = var.image_digest == "" || can(regex("^sha256:[a-f0-9]{64}$", var.image_digest))
    error_message = "image_digest must be empty or sha256:<64 hex chars>."
  }
}

variable "image_tag" {
  description = "Image tag used when image_digest is empty."
  type        = string
  default     = "3.0.0"
}

variable "profile" {
  description = "HaaS profile. 'prod' enables fail-closed validation."
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["dev", "prod"], var.profile)
    error_message = "profile must be 'dev' or 'prod'."
  }
}

variable "values_files" {
  description = "Additional Helm values files (contents), applied after the generated values."
  type        = list(string)
  default     = []
}

variable "keda_enabled" {
  description = "Use KEDA ScaledObjects on Celery queue length for workers (KEDA must be installed)."
  type        = bool
  default     = false
}

# --- Managed data services (provider-agnostic inputs) -------------------------

variable "postgres" {
  description = <<-EOT
    Managed PostgreSQL endpoints. Three distinct logins are required:
    api_dsn (haas_api, member of haas_tenant_app), executor_dsn (haas_executor, member of
    haas_execution_service) and migration_dsn (schema owner, used only by the migration hook).
    cidrs restricts NetworkPolicy egress to the database subnets.
  EOT
  type = object({
    api_dsn       = string
    executor_dsn  = string
    migration_dsn = string
    cidrs         = list(string)
    port          = optional(number, 5432)
  })
  sensitive = true
}

variable "redis" {
  description = <<-EOT
    Managed Redis. url/broker_url/result_backend_url are full redis:// or rediss:// URLs
    (DB 0 events/secrets, DB 1 Celery broker, DB 2 results by convention). address is
    host:port for the KEDA scaler; password is optional (KEDA TriggerAuthentication).
  EOT
  type = object({
    url                = string
    broker_url         = string
    result_backend_url = string
    address            = string
    broker_db_index    = optional(number, 1)
    tls                = optional(bool, true)
    password           = optional(string, "")
    cidrs              = list(string)
    port               = optional(number, 6379)
  })
  sensitive = true
}

variable "object_store" {
  description = "S3-compatible object store for artifacts. Leave null to disable."
  type = object({
    endpoint          = string
    bucket            = string
    region            = optional(string, "")
    access_key_id     = optional(string, "")
    secret_access_key = optional(string, "")
  })
  default   = null
  sensitive = true
}

variable "object_store_env_names" {
  description = "Environment variable names the application reads for object-store settings."
  type        = map(string)
  default = {
    endpoint          = "HAAS_OBJECT_STORE_ENDPOINT"
    bucket            = "HAAS_OBJECT_STORE_BUCKET"
    region            = "HAAS_OBJECT_STORE_REGION"
    access_key_id     = "HAAS_OBJECT_STORE_ACCESS_KEY_ID"
    secret_access_key = "HAAS_OBJECT_STORE_SECRET_ACCESS_KEY"
  }
}

# --- Application credentials ---------------------------------------------------

variable "create_secrets" {
  description = <<-EOT
    Create the Kubernetes Secrets from the sensitive variables below. Values then live in
    Terraform state: use an encrypted remote backend, or set false and provision the Secrets
    with External Secrets / Vault using the names in the chart values.
  EOT
  type        = bool
  default     = false
}

variable "oidc" {
  description = "OIDC issuer/audience/JWKS URL (HTTPS) required by the prod API."
  type = object({
    issuer   = string
    audience = string
    jwks_url = string
  })
  default = null
}

variable "secret_encryption_key" {
  description = "Base64 32-byte AES key for the secret vault (HAAS_SECRET_ENCRYPTION_KEY)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "envelope_signing_key" {
  description = "Ed25519 seed signing tool envelopes (orchestrator only)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "envelope_verify_key" {
  description = "Ed25519 public key verifying tool envelopes (tool workers only)."
  type        = string
  default     = ""
}

variable "openrouter_api_key" {
  description = "Model-provider API key (orchestrator only)."
  type        = string
  default     = ""
  sensitive   = true
}

# --- Networking ----------------------------------------------------------------

variable "api_ingress_namespaces" {
  description = "Namespaces allowed to reach the API pods (ingress controller, monitoring)."
  type        = list(string)
  default     = ["ingress-nginx"]
}

variable "api_hostname" {
  description = "Public API hostname. Empty disables the Ingress."
  type        = string
  default     = ""
}

variable "ingress_class_name" {
  description = "IngressClass for the API."
  type        = string
  default     = "nginx"
}

variable "tls_secret_name" {
  description = "TLS Secret for the API Ingress (e.g. issued by cert-manager)."
  type        = string
  default     = "haas-api-tls"
}

variable "tool_worker_runtime_class" {
  description = "Optional RuntimeClass (gvisor, kata) for tool workers."
  type        = string
  default     = ""
}
