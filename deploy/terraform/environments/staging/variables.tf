variable "kubeconfig_path" {
  type    = string
  default = "~/.kube/config"
}

variable "kube_context" {
  type    = string
  default = null
}

variable "image_repository" {
  type = string
}

variable "image_digest" {
  type    = string
  default = ""
}

variable "image_tag" {
  type    = string
  default = "3.0.0"
}

variable "keda_enabled" {
  type    = bool
  default = true
}

variable "create_secrets" {
  type    = bool
  default = false
}

variable "postgres" {
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

variable "oidc" {
  type = object({
    issuer   = string
    audience = string
    jwks_url = string
  })
  default = null
}

variable "secret_encryption_key" {
  type      = string
  default   = ""
  sensitive = true
}

variable "envelope_signing_key" {
  type      = string
  default   = ""
  sensitive = true
}

variable "envelope_verify_key" {
  type    = string
  default = ""
}

variable "openrouter_api_key" {
  type      = string
  default   = ""
  sensitive = true
}

variable "api_hostname" {
  type    = string
  default = ""
}

variable "tool_worker_runtime_class" {
  type    = string
  default = ""
}
