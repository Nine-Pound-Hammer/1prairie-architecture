# Application-level alerts: log-based metrics on the structured `event=*` keys
# the API service emits, each with its own threshold and runbook anchor.
#
#   - cross-tenant probe          security; ANY hit pages
#   - event=storage_orphan        cleanup noise; threshold-gated
#   - event=published_menu_teardown_failed  cleanup noise; threshold-gated
#   - event=ai_capacity_exhausted Vertex AI turned a user away after retries
#
# The keys are pinned by unit tests in the API service, so a refactor that
# changes a log format breaks a test instead of silently breaking an alert.

locals {
  # `severity>=WARNING` keeps an INFO-level mention of a key (a quoted doc
  # comment, an echoed test fixture) from tripping the alert. The resource pin
  # keeps a stray line in another service from false-alarming.
  #
  # `textPayload:"<key>"` is a substring match: Cloud Run captures Python
  # `logging` output as textPayload. If the service moves to a structured
  # handler, the filter flips to `jsonPayload.message:"<key>"`.
  base_filter = "resource.type=\"cloud_run_revision\" resource.labels.service_name=\"${var.api_service_name}\" severity>=WARNING"

  log_metric_filters = {
    storage_orphan                 = "${local.base_filter} textPayload:\"event=storage_orphan\""
    published_menu_teardown_failed = "${local.base_filter} textPayload:\"event=published_menu_teardown_failed\""
    cross_tenant_probe             = "${local.base_filter} textPayload:\"cross-tenant probe\""
    ai_capacity_exhausted          = "${local.base_filter} textPayload:\"event=ai_capacity_exhausted\""
  }

  # With ALIGN_DELTA, `alignment_period` is also the evaluation window: each
  # point is the count of matching entries in the preceding window. duration =
  # 0s fires as soon as a window crosses the threshold.
  alert_policies = {
    storage_orphan = {
      display_name     = "api: event=storage_orphan WARNING > 5/hr"
      threshold_value  = 5
      alignment_period = "3600s"
      runbook_anchor   = "storage_orphan"
      docs_summary     = "Account deletion failed to remove a Storage object. Sweep stragglers; usually transient."
    }
    published_menu_teardown_failed = {
      display_name     = "api: event=published_menu_teardown_failed > 5/hr"
      threshold_value  = 5
      alignment_period = "3600s"
      runbook_anchor   = "published_menu_teardown_failed"
      docs_summary     = "Account deletion failed to remove a published menu object. Sweep stragglers if the pattern persists."
    }
    # Threshold 0: any single ownership 403 may be an active scan of
    # guessable tenant ids.
    cross_tenant_probe = {
      display_name     = "api: cross-tenant probe (SECURITY)"
      threshold_value  = 0
      alignment_period = "300s"
      runbook_anchor   = "cross-tenant-probe"
      docs_summary     = "403 because the resource's ownerId != caller uid. Could be a credential leak or an active scan."
    }
    # In-process retries absorb occasional blips without logging this key, so
    # more than a few per hour means the capacity shortage is widespread.
    # These used to be 502s, caught by the 5xx-rate alert; returning them as
    # 429s moved them out of that alert's view, hence a dedicated policy.
    ai_capacity_exhausted = {
      display_name     = "api: event=ai_capacity_exhausted > 3/hr"
      threshold_value  = 3
      alignment_period = "3600s"
      runbook_anchor   = "ai_capacity_exhausted"
      docs_summary     = "Vertex AI was out of capacity (429/503) on menu extraction after retries; users saw a 'busy' message."
    }
  }
}

resource "google_monitoring_notification_channel" "email" {
  project      = var.project_id
  display_name = "Primary on-call email"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }

  depends_on = [google_project_service.enabled]
}

resource "google_logging_metric" "alert" {
  for_each = local.log_metric_filters

  project     = var.project_id
  name        = "${var.api_service_name}/${replace(each.key, "_", "-")}"
  filter      = each.value
  description = "Counter for the '${each.key}' alert key emitted by ${var.api_service_name}."

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }

  depends_on = [google_project_service.enabled]
}

resource "google_monitoring_alert_policy" "alert" {
  for_each = local.alert_policies

  project      = var.project_id
  display_name = each.value.display_name
  combiner     = "OR"

  conditions {
    display_name = "count > ${each.value.threshold_value} in ${each.value.alignment_period}"

    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.alert[each.key].name}\" resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = each.value.threshold_value
      duration        = "0s"

      aggregations {
        alignment_period   = each.value.alignment_period
        per_series_aligner = "ALIGN_DELTA"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]

  documentation {
    content   = "${each.value.docs_summary}\n\nRunbook: ${var.runbook_base_url}#${each.value.runbook_anchor}"
    mime_type = "text/markdown"
  }

  # Close stale incidents after 24h instead of the 7-day default: if the
  # metric stops reporting (code path removed, rollback) the incident would
  # otherwise linger.
  alert_strategy {
    auto_close = "86400s"
  }
}
