# 0005 — AI provider: Google Vertex AI

- **Status:** Accepted
- **Date:** 2026-04-26

> Published copy, lightly edited for an outside reader: references to other ADRs and internal paths are summarized in plain text, and identifiers are removed. The decision and its reasoning are unchanged.

## Context

AI is core to the menu product, not adjacent. The primary authoring UX is conversational; ingestion (PDF / photo → structured menu), description generation, allergen tagging, SEO metadata, and natural-language bulk operations all flow through an LLM. The agent surface is the product, not a feature on the side.

An earlier decision (ADR-0002, identity and project topology) put the project inside Google Cloud: a Workspace organization, a dev/prod GCP project pair, Firebase, and Secret Manager. The choice of AI provider was the largest open architecture decision in the product requirements. The two realistic options:

- **Anthropic API directly.** Strong conversational quality and tool use; separate cloud surface from GCP, with its own billing, auth, secret rotation, observability, and incident pages.
- **Google Vertex AI.** Gemini family models, plus Anthropic Claude available as first-class third-party models via the Vertex Model Garden. Native GCP integration: IAM, billing, Cloud Logging, Cloud Monitoring, Workload Identity Federation, VPC controls.

## Decision

Use **Google Vertex AI** for all LLM workloads in the menu product (and, by default, for future products).

- **Default model family: Gemini** (current generation), accessed via Vertex's standard endpoints.
- **Fallback: Claude via Vertex Model Garden** for specific workloads where Gemini quality demonstrably underperforms.
- **No second cloud** for AI: if a workload requires Claude, it goes through Vertex's Model Garden, not the Anthropic API directly.

This is consistent with the broader principle of staying on Google's stack as much as possible: one cloud, one identity domain, one bill, one observability surface, one set of access controls.

## Alternatives considered

- **Anthropic API directly.** Rejected. The advantage in raw conversational quality and tool-use polish is real but narrowing as Gemini iterates, and Vertex's Model Garden makes Claude available natively when a specific workload demands it. Going direct would mean a second identity surface, a second secret-rotation pipeline, a second billing relationship, parallel observability dashboards, and more drift between dev/prod identity and CI auth. The reduction in operational surface from staying single-cloud outweighs the raw model-quality gap.
- **Multi-provider abstraction layer up front.** Rejected per the project's code-sharing philosophy (ADR-0004: extract an abstraction when duplication appears, not in advance). Build for one provider; if a second becomes necessary, extract the abstraction from observed needs rather than speculation. Provider-abstracted AI clients tend to expose only the lowest-common-denominator API and lose access to provider-specific features (caching, structured outputs, native tool calling).
- **Self-host open-weight models** (Llama, Qwen, Mistral). Rejected. Operational overhead — GPU procurement, autoscaling, model serving, eval pipeline — far exceeds any conceivable benefit at MVP scale. Revisit only if usage scales to where the Vertex bill rivals one or two dedicated GPUs running 24/7, which is unlikely in this product's lifetime.

## Consequences

- AI calls authenticate via Application Default Credentials and service accounts inside the same GCP project. No separate API keys to manage in Secret Manager beyond what GCP already requires.
- Cost shows up on the same GCP bill as the rest of the project, simplifying budget alerts and per-tenant cost tracking.
- Logs and traces flow into Cloud Logging and Cloud Monitoring alongside Firestore, Hosting, and the API — single dashboard, single incident workflow.
- Workload Identity Federation (the intended CI posture) is uniform across infrastructure: CI authenticates to GCP and reaches Vertex through the same federation, with no parallel Anthropic-API key path.
- Tied to Google's model release cadence and pricing. Mitigated by Model Garden hosting third-party models as first-class endpoints; switching the default model is a code change in one AI module, not an infrastructure migration.
- Customer data stays inside the GCP perimeter end-to-end (Cloud Storage → Vertex → Firestore). Data-handling claims and any future compliance posture are simpler when no data leaves Google.
- Prompt design, tool-calling shape, and structured-output contracts will assume Vertex API conventions. Migrating to Anthropic-direct in the future is feasible but non-trivial; acceptable trade — the integration surface is bounded to one module.
