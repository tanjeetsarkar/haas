output "namespace" {
  description = "Namespace the release runs in."
  value       = local.namespace
}

output "release_name" {
  description = "Helm release name."
  value       = helm_release.haas.name
}

output "api_service" {
  description = "In-cluster API service DNS name."
  value       = "${helm_release.haas.name}-haas-api.${local.namespace}.svc.cluster.local:8000"
}

output "managed_secret_names" {
  description = "Secrets created by this module (empty when create_secrets = false)."
  value       = sort([for s in kubernetes_secret_v1.haas : s.metadata[0].name])
}
