# The production module enables every API the project uses in one place.
# Only the two this sample depends on are shown.
resource "google_project_service" "enabled" {
  for_each = toset([
    "logging.googleapis.com",
    "monitoring.googleapis.com",
  ])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}
