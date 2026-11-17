# Terraform monitoring for Cloud Run

Alerting for a set of Cloud Run services, Firestore and Vertex AI, adapted from the production Terraform module. One module instance per environment; the platform alerts switch on in prod only.

## What it shows

- **Two layers of signals.**
  - `monitoring_app.tf` turns stable log keys that the API emits (`event=…`) into log-based metrics and alert policies.
  - `monitoring_platform.tf` covers the platform: uptime from several regions, 5xx ratio, p99 latency, instance count, and a Firestore read-rate cost canary.
- **Alerts tied to code.** The log keys are pinned by unit tests in the API service, so changing a log format breaks a test rather than silently disabling an alert. The ingestion sample in this repo emits two of these keys.
- **Thresholds that reflect intent.**
  - A cross-tenant access attempt pages on the first hit.
  - Cleanup noise is batched to more than 5 per hour.
  - AI capacity failures alert at more than 3 per hour, because in-process retries already absorb single blips.
- **Avoiding false alarms on purpose.**
  - Idle services produce no ratio point.
  - Internal tools that scale to zero are excluded from the latency alert.
  - Uptime alerts need more than one probe region to fail.
  - Stale incidents auto-close after 24 hours.
- **Data-driven resources.** Each app-level alert is one map entry. `for_each` generates the metric and the policy, so adding an alert is a two-line change.
- **Every alert links to its runbook section.**

## Files

- `monitoring_app.tf`: log-based metrics and alerts.
- `monitoring_platform.tf`: uptime, 5xx, latency, scale and cost alerts.
- `variables.tf`: inputs. The alert email is a variable, never committed.
- `apis.tf` and `versions.tf`: the minimum needed for the sample to stand alone.

`terraform fmt -check` and `terraform validate` pass on this directory. It isn't wired to a backend or a real project.
