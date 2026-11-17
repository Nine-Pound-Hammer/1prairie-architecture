# Platform-level alerts: is each service answering, and is it healthy?
#
#   - web `/` and api `/health` uptime, failing from more than one region
#   - 5xx rate > 1% over 5 min, per service
#   - p99 request latency > 3s over 10 min, per public service
#   - Cloud Run instance count > 5 (early scale or runaway signal)
#   - Firestore document reads > 100k/hr (cost-spike canary)
#
# Gated on var.monitoring_alerts_enabled (prod only): uptime checks need the
# public custom domains that only exist in prod, and dev's experimental
# traffic would trip the 5xx and latency alerts constantly.

locals {
  uptime_targets = {
    web = {
      display_name = "web apex (/)"
      host         = var.base_domain
      path         = "/"
      # A canonical-host redirect is a healthy response, not an outage.
      accept_3xx = true
    }
    api = {
      display_name = "api (/health)"
      host         = "api.${var.base_domain}"
      # `/healthz` is intercepted by the Cloud Run frontend before it reaches
      # the app, so the liveness endpoint is `/health`.
      path       = "/health"
      accept_3xx = false
    }
  }
}

# --- Uptime checks --------------------------------------------------------

resource "google_monitoring_uptime_check_config" "cloud_run" {
  for_each = var.monitoring_alerts_enabled ? local.uptime_targets : {}

  project      = var.project_id
  display_name = each.value.display_name
  timeout      = "10s"
  period       = "300s"

  http_check {
    path         = each.value.path
    port         = 443
    use_ssl      = true
    validate_ssl = true

    accepted_response_status_codes {
      status_class = "STATUS_CLASS_2XX"
    }

    dynamic "accepted_response_status_codes" {
      for_each = each.value.accept_3xx ? [1] : []
      content {
        status_class = "STATUS_CLASS_3XX"
      }
    }
  }

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = each.value.host
    }
  }

  depends_on = [google_project_service.enabled]
}

resource "google_monitoring_alert_policy" "uptime" {
  for_each = var.monitoring_alerts_enabled ? local.uptime_targets : {}

  project      = var.project_id
  display_name = "Uptime down: ${each.value.display_name}"
  combiner     = "OR"

  conditions {
    display_name = "Uptime check failing from >1 probe region"

    condition_threshold {
      filter = join(" ", [
        "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\"",
        "resource.type=\"uptime_url\"",
        "metric.label.check_id=\"${google_monitoring_uptime_check_config.cloud_run[each.key].uptime_check_id}\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "60s"

      # ALIGN_NEXT_OLDER + REDUCE_COUNT_FALSE = how many probe regions reported
      # a failed check in the window. Requiring more than one region is the
      # multi-region form of "not a one-off blip".
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }

      trigger {
        count = 1
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  documentation {
    content   = "Uptime check for ${each.value.host}${each.value.path} is failing from more than one probe region.\n\nRunbook: ${var.runbook_base_url}#cloud-run-uptime"
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "86400s"
  }
}

# --- 5xx error rate > 1% over 5 min, per service --------------------------

resource "google_monitoring_alert_policy" "cloud_run_5xx" {
  count = var.monitoring_alerts_enabled ? 1 : 0

  project      = var.project_id
  display_name = "Cloud Run: 5xx rate > 1% (5 min)"
  combiner     = "OR"

  conditions {
    display_name = "5xx responses exceed 1% of requests per service"

    condition_threshold {
      # A ratio per service. An idle service produces no point (0/0 is
      # dropped, not reported as 0), so it can't false-alarm.
      filter = join(" ", [
        "metric.type=\"run.googleapis.com/request_count\"",
        "resource.type=\"cloud_run_revision\"",
        "metric.label.response_code_class=\"5xx\"",
      ])
      denominator_filter = join(" ", [
        "metric.type=\"run.googleapis.com/request_count\"",
        "resource.type=\"cloud_run_revision\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 0.01
      duration        = "300s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
      denominator_aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  documentation {
    content   = "A Cloud Run service is returning 5xx for more than 1% of requests over 5 minutes. The incident names the service.\n\nRunbook: ${var.runbook_base_url}#cloud-run-5xx"
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "86400s"
  }
}

# --- p99 request latency > 3s over 10 min, per public service -------------

resource "google_monitoring_alert_policy" "cloud_run_latency" {
  count = var.monitoring_alerts_enabled ? 1 : 0

  project      = var.project_id
  display_name = "Cloud Run: p99 latency > 3s (10 min)"
  combiner     = "OR"

  conditions {
    display_name = "p99 request latency exceeds 3s per service"

    condition_threshold {
      # Public services only. An internal tool that scales to zero lets one
      # cold start dominate p99 and page on an ordinary admin login.
      filter = join(" ", [
        "metric.type=\"run.googleapis.com/request_latencies\"",
        "resource.type=\"cloud_run_revision\"",
        "resource.label.service_name=one_of(${join(",", [for s in var.latency_slo_services : "\"${s}\""])})",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 3000 # request_latencies is reported in ms
      duration        = "600s"

      # p99 per revision, then the worst revision per service, so a bad new
      # rollout isn't averaged away by a healthy one mid-migration.
      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_PERCENTILE_99"
        cross_series_reducer = "REDUCE_MAX"
        group_by_fields      = ["resource.label.service_name"]
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  documentation {
    content   = "A Cloud Run service's p99 request latency exceeded 3s over 10 minutes. The incident names the service.\n\nRunbook: ${var.runbook_base_url}#cloud-run-latency"
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "86400s"
  }
}

# --- Cloud Run instance count > 5 (scale / runaway signal) ----------------

resource "google_monitoring_alert_policy" "cloud_run_instances" {
  count = var.monitoring_alerts_enabled ? 1 : 0

  project      = var.project_id
  display_name = "Cloud Run: instance count > 5"
  combiner     = "OR"

  conditions {
    display_name = "Active+idle instances exceed 5 per service"

    condition_threshold {
      # At current volume, more than five instances is far more likely bot
      # traffic or a regression than real demand.
      filter = join(" ", [
        "metric.type=\"run.googleapis.com/container/instance_count\"",
        "resource.type=\"cloud_run_revision\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      duration        = "300s"

      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_MAX"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  documentation {
    content   = "A Cloud Run service held more than 5 instances for 5 minutes. Suspect bot traffic or a regression before real demand.\n\nRunbook: ${var.runbook_base_url}#cloud-run-instance-count"
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "86400s"
  }
}

# --- Firestore document reads > 100k/hr (cost-spike canary) ---------------

resource "google_monitoring_alert_policy" "firestore_reads" {
  count = var.monitoring_alerts_enabled ? 1 : 0

  project      = var.project_id
  display_name = "Firestore: document reads > 100k/hr"
  combiner     = "OR"

  conditions {
    display_name = "Hourly Firestore read ops exceed 100k"

    condition_threshold {
      # A billing canary: a runaway query, a hot loop, or a missing index
      # forcing collection scans shows up as a read-count cliff. The Monitoring
      # API rejects this metric without the resource.type pin.
      filter          = "metric.type=\"firestore.googleapis.com/document/read_count\" resource.type=\"firestore_instance\""
      comparison      = "COMPARISON_GT"
      threshold_value = 100000
      duration        = "0s"

      aggregations {
        alignment_period     = "3600s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  documentation {
    content   = "Firestore served more than 100,000 document reads in an hour. Suspect a runaway query, a hot loop, or a missing index.\n\nRunbook: ${var.runbook_base_url}#firestore-read-spike"
    mime_type = "text/markdown"
  }

  alert_strategy {
    auto_close = "86400s"
  }
}
