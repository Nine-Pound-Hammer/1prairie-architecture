# Architecture notes

Deeper notes behind the [README](../README.md). Identifiers are generic; collection names show the shape of the data model, not real tenants.

## Environments

- Each product gets two GCP projects, dev and prod, built from one Terraform module. They share nothing at runtime, so a mistake in dev can't reach production data.
- Dev deploys from the integration branch. Prod deploys from the main branch, which is advanced only by a reviewed promotion.
- Remote Terraform state is versioned in Cloud Storage with public access blocked.
- Every project has a budget with alerts.

## Services

| Service | Runtime | Role |
|---|---|---|
| Owner web app | Next.js on Cloud Run | Sign-up, menu editing, billing, publishing. Talks to Firebase directly for reads and to the API for authoritative writes. |
| API | FastAPI on Cloud Run | Tenant creation and slug claims, AI ingestion, publishing, account deletion, Stripe webhooks, transactional email. Runs as its own least-privilege service account. |
| Edge server | FastAPI on Cloud Run | Serves published menus by `Host` header from Cloud Storage. Read-only, no secrets, no database access. |
| Internal admin | Next.js on Cloud Run | Previews and prospecting tools. Reachable only through Cloud IAP with a Workspace identity. |
| Prospecting pipeline | Airflow 3 | Discovery, deduplication, BigQuery load and LLM enrichment, fed by Pub/Sub. |
| Marketing site | Astro on Firebase Hosting | Product site and blog. |

## Data model

Firestore, one document tree per restaurant:

```
users/{uid}                         -> { restaurantId, ... }
restaurants/{restaurantId}          -> { ownerId, slug, aiUsage{...}, publishedUrl, ... }
  members/{uid}                     -> membership
  sections/{sectionId}              -> { name, order }
  items/{itemId}                    -> { sectionId, name, description, price, order, available }
  ingest_drafts/{draftId}           -> { status, pages[], parsedMenu, ... }
  rate_limit_events/{eventId}       -> { uid, createdAt, expiresAt }   (TTL-purged)
restaurant_slugs/{slug}             -> { restaurantId, ownerId, claimedAt }
```

- **Items are flat under the restaurant,** with a `sectionId` and an `order`, rather than nested under sections. Reordering or moving an item is a single-document write, and the whole menu loads with two queries.
- **`restaurant_slugs` is a uniqueness index.** Firestore has no unique constraints, so the slug is claimed by creating a document whose id is the slug. That happens inside the same transaction that creates the restaurant ([ADR-0010](adr/0010-restaurant-slug-uniqueness-and-reserved-words.md)).
- **Drafts are separate from the live menu.** AI output is never written straight into `sections` and `items`. It sits in `ingest_drafts` until the owner accepts it, and acceptance is one atomic batch.

## Tenancy and authorization

There are three layers, and each one assumes the layer above it can fail.

1. **Security rules.** Firestore and Storage rules restrict clients to their own tenant's documents and their own upload paths, and deny client writes to anything authoritative (slug claims, tenant creation). The rules have their own emulator test suite.
2. **API checks.** Any write that could cross tenants goes through the API. The API verifies the Firebase ID token, resolves the caller's restaurant, checks `ownerId`, and validates client-written ids against path injection before they're interpolated into a document path.
3. **Detection.** A failed ownership check logs a stable key. A log-based metric counts it, and an alert pages on the first occurrence.

Internal tools use a separate identity system altogether (Cloud IAP with Workspace accounts). A problem with customer sign-in can't take them down, and they can't be reached with a customer account.

## Publishing

1. Publish renders the menu to static HTML and writes it to a Cloud Storage bucket keyed by slug, with `Cache-Control: no-cache`.
2. A global HTTPS load balancer with a Certificate Manager wildcard certificate sends every `<slug>.<menu-domain>` to the edge service through a serverless network endpoint group, with Cloud CDN in front. The CDN cache key includes the host.
3. The edge service maps host to slug to object and returns it with its ETag. The CDN revalidates on each request, so a republish is visible immediately without an invalidation call.

Firebase Hosting can't issue wildcard certificates, and a URL map can't capture a host label into a path. That's why a small compute service sits behind the load balancer.

## AI usage and cost tracking

- **Per call:** every Gemini call writes one structured log line with tenant id, restaurant id, operation, model, page count and success. Cloud Logging indexes those fields, so usage per tenant or per model is a log query.
- **Per tenant:** the restaurant document carries an `aiUsage` counter, incremented atomically on each attempt, failures included.
- **Rate limits:** sliding windows per user (a short burst window and a monthly window) and per restaurant (monthly). They're backed by an event collection whose documents Firestore deletes automatically with a TTL policy.
- **Cost:** Vertex AI spend is on the same GCP bill as everything else ([ADR-0005](adr/0005-ai-provider-vertex-ai.md)). The log fields are designed to join with the billing export in BigQuery by time and project, to attribute spend to tenants.
- **Controls:**
  - Size, type and rate checks run before the paid call.
  - Model "thinking" is held low for extraction.
  - The model is an environment setting, so it can be changed or rolled back without a code change.
  - An alert fires when users are turned away for lack of capacity.
- **Pipeline:** the prospecting DAG logs an estimated model cost for each enrichment run.

## Analytics

GA4 covers the owner app and the public pages, with consent mode on public surfaces. The native GA4 → BigQuery export gives unsampled, event-level data for funnels, cohorts and source analysis in SQL. Data accrues only from the date the export is linked, so it was linked before launch.
