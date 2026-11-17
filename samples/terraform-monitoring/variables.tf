variable "project_id" {
  description = "GCP project the alerts live in (one module instance per environment)."
  type        = string
}

variable "alert_email" {
  description = "Address the notification channel delivers to. Supplied per environment, never committed."
  type        = string
  sensitive   = true
}

variable "base_domain" {
  description = "Public domain the web app is served at; the API is served at api.<base_domain>."
  type        = string
}

variable "api_service_name" {
  description = "Cloud Run service that emits the structured event=* log keys."
  type        = string
  default     = "api"
}

variable "latency_slo_services" {
  description = "Public, user-facing Cloud Run services held to the p99 latency alert."
  type        = list(string)
  default     = ["web", "api", "edge"]
}

variable "runbook_base_url" {
  description = "Base URL of the alerts runbook; each alert links to its own anchor."
  type        = string
}

variable "monitoring_alerts_enabled" {
  description = "Platform alerts (uptime, 5xx, latency, scale, cost canary). On in prod only."
  type        = bool
  default     = false
}
