# 0012 — Terraform for project-level GCP/Firebase infrastructure

- **Status:** Accepted
- **Date:** 2026-05-21

> Published copy, lightly edited for an outside reader: GCP project IDs and the state bucket are placeholders (`example-dev`, `example-prod`, `example-tfstate`), issue links are removed, and links to other ADRs and runbooks are plain text. The decision and its reasoning are unchanged.

## Context

ADR-0002 committed the business to a per-product, per-environment GCP project topology: today `example-dev` and (imminent) `example-prod`; in the near future a second product's dev/prod pair. Each project needs the same bring-up — enabled APIs, service accounts and IAM, Cloud Build triggers, Artifact Registry, Firebase project + Web App, Firestore database, Secret Manager containers, the Hosting site (prod only), the per-project budget — and most of that work is *the same* across projects.

How we've done it so far is by hand, twice, against a checklist: the new-GCP-project runbook. That worked for `example-dev`, but the bring-up surfaced specific failure modes the checklist alone can't prevent:

- **Silent IAM gotchas.** `storage.rules` calls `firestore.exists()` to gate uploads, which requires `roles/firebaserules.firestoreServiceAgent` on the Firebase Rules service agent. Firebase usually auto-binds this but has been observed to skip it when Storage is initialized after Firestore — runbook step 7 documents the manual fix, but "remember to verify step 7" is exactly the kind of instruction that ages badly under stress (a botched prod cutover) or rotation (a future second person).
- **Drift between dev and prod.** The runbook patches itself when a step changes (e.g., `aiplatform.googleapis.com` was added to step 2 only after the dev bring-up was already done). There's no enforcement that the next project will use the patched version.
- **One-product, one-person assumption.** The runbook is a literate checklist whose audience is "the sole engineer, in a focused afternoon." It does not survive long context gaps, and a third project would amount to a third run of the same imperative work.

The decision is whether to absorb the cost of declarative infrastructure management now — when the catalogue is small and changes are still fresh — or to defer it until pain shows up later, when the catalogue is larger and pain is more expensive.

## Decision

**Adopt Terraform now, scoped tight.** A reusable `product-project` module captures the parts of a product project that survive past day one; per-environment instantiations live in `infra/terraform/envs/<env>/`. Bring-up of `example-prod` and any future product's project pair runs through `terraform apply` rather than the runbook. The runbook stays as a verification checklist and a fallback when something has no first-class TF resource yet.

### In scope (Terraform owns)

- Enabled APIs (`apis.tf`)
- Cloud Build triggers and the build SA's project-level role set (`cloud_build.tf`, `service_accounts.tf`)
- Resource-scoped `roles/iam.serviceAccountUser` so the build SA can act-as only the runtime SAs we actually deploy with
- Artifact Registry repository (`artifact_registry.tf`)
- Service accounts: the Cloud Build SA bindings, plus a dedicated API runtime SA with least-privilege Vertex / Firestore / Storage / token-creator roles (`service_accounts.tf`)
- Secret Manager **secret containers** (names only — values seeded out-of-band by a seeding script and `gcloud secrets versions add`)
- Firestore database, with PITR + `prevent_destroy` lifecycle on prod, and the `firebaserules.firestoreServiceAgent` binding made explicit so step 7 can't be forgotten
- Firebase project enrolment and Web App registration; the published-menu Hosting site (prod only, per ADR-0006)
- Per-project monthly budget (alerts-only — no hard cap, per ADR-0002)
- App-level Cloud Logging alert policies — the structured `event=*` keys emitted by the API service (`event=storage_orphan`, `event=hosting_teardown_failed`, `cross-tenant probe`) and their log-based metrics + notification channel. Adding another app-level key means adding two map entries in `monitoring.tf` — same module-template shape as the others. Uptime / 5xx / p99-latency / Firestore-cost-canary alerts remain Console-managed for now and may migrate here later.

### Out of scope (managed elsewhere — drift fight we don't want)

- **Cloud Run revisions** — owned by Cloud Build. Terraform would race with every deploy.
- **Secret values** — out-of-band via the Stripe dashboard, `gcloud secrets versions add`, and the seeding script. Keeping values out of state is non-negotiable (state ends up in a GCS bucket and audit logs).
- **Firestore rules + indexes** — `firestore.rules` and `firestore.indexes.json` via `firebase deploy`. Terraform's `google_firestore_index` exists but the workflow is materially worse than `firebase deploy`.
- **Firebase Storage rules** — `storage.rules` via `firebase deploy`.
- **Firebase Storage default bucket initialization** — no first-class TF resource at the time of writing; remains a Console step in the runbook.
- **Cloud Run custom domain mappings + Firebase Auth authorized-domain list** — interacts awkwardly with provisional cert state; the runbook handles these imperatively for now.
- **Platform monitoring alerts** — Cloud Run uptime / 5xx / p99-latency / Firestore-read-rate alerts stay Console-managed for now. The app-level `event=*` key alerts ARE Terraform-managed (see in-scope list above) — split because the app-level keys are stable, code-pinned, and identical across dev/prod, whereas the uptime-check shape is still settling.

### State

A single state bucket `gs://example-tfstate` in `example-dev` (versioning on, public access locked, uniform bucket-level access). State per environment lives under `env/<env>/`. Created one-time by a bootstrap script.

### Adoption sequence

1. Run the state bootstrap script (one-time).
2. Write `import` blocks for resources already in `example-dev`. Iterate `terraform plan` until clean.
3. `terraform apply` in `envs/dev/` — ideally a no-op apply that just acknowledges the imported state.
4. `terraform apply` in `envs/prod/` **creates** `example-prod` from scratch. This is the moment Terraform earns its keep: the prod project is provably built from the same module that produced dev.

## Alternatives considered

**Continue runbook-only.** Cheapest now; loses to drift every time the catalogue grows. The trigger that moved this decision off the "later" pile was the second product already in planning: running the same imperative bring-up for a third project is the failure mode the runbook can't fix.

**Pulumi.** Same declarative model, language-native (Python or TypeScript) rather than HCL. Rejected because Terraform's GCP provider is the more battle-tested of the two for the surface we touch (Firebase + Cloud Run + Cloud Build), and because HCL's narrow surface is a feature for infra: it discourages clever code in a domain where clever code becomes a state-management problem.

**GCP Config Connector / Config Controller.** Kubernetes-native, declarative GCP resources. Rejected because it requires a GKE cluster to run — we don't operate a GKE cluster and don't intend to.

**`gcloud` scripts checked into the repo.** A halfway house: imperative but version-controlled. Rejected because the state-reconciliation gap (what's in the script vs. what's actually in the project) is exactly the gap Terraform closes. A scripted bring-up is still a runbook with extra lint.

## Consequences

**Becomes easier:**

- The step-7 IAM gotcha is declarative — `terraform plan` shows it every time, "remember to do it" becomes "remember to remove it from the plan."
- A second product project pair is one new `envs/<product>-{dev,prod}/` directory pointing at the same module, not a third runbook execution.
- Infra changes review like code: diffs, plans, and PRs. The "we updated the runbook to add `aiplatform`, but who knows whether it's actually enabled where" problem goes away.
- `example-prod` bring-up gets to verify dev as a side effect — `terraform plan` against dev is now part of the workflow.

**Becomes harder / trade-offs accepted:**

- **Adoption cost.** Importing `example-dev` is real work — every existing resource needs an `import` block until `plan` is clean. Estimated one focused session.
- **State file ownership.** A GCS bucket needs to exist forever. Backups (versioning) are free; "what happens if state diverges from reality" is now an operational concern that wasn't before.
- **Provider upgrades.** `hashicorp/google` major bumps periodically force resource-schema migrations. Realistically once or twice a year for a project this size.
- **Two ways to change one thing.** Cloud Build SA roles can now drift between the module and a manual `gcloud projects add-iam-policy-binding`. Mitigated by `terraform plan` running before every apply, but the discipline is real.
- **Some things still imperative.** Storage bucket init, custom domains, platform monitoring alerts, secret values — see "out of scope" above. The runbook doesn't go away; it shrinks.
- **Bus factor.** A future second engineer (or returning future self after a long gap) has to be Terraform-literate to ship infra changes. Acceptable cost — Terraform is a portable, employable skill.

**Reversibility.** If this turns out to be the wrong call (e.g., second product is shelved and the runbook never breaks again), the exit is `terraform state rm <everything>` plus deleting `infra/terraform/`. The live infrastructure is untouched. The lost cost is the time spent on imports — not a state-mutation we can't undo.
