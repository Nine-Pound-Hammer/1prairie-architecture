# Architecture decision records

The private codebase has 23 ADRs, written as each decision was made and kept in the repository next to the code. Three are published here. Their numbers and dates are the originals; the text is lightly edited so it reads without the rest of the repository (placeholders for identifiers, plain-text references to ADRs that aren't included).

| ADR | Date | Decision | Why it's here |
|---|---|---|---|
| [0005](0005-ai-provider-vertex-ai.md) | 2026-04-26 | Vertex AI (Gemini) for every LLM workload, not the Anthropic API directly | Choosing one cloud for identity, billing, logging and data residency over a marginal model-quality edge. |
| [0010](0010-restaurant-slug-uniqueness-and-reserved-words.md) | 2026-05-07 | Enforce slug uniqueness with a parallel collection and a server-side Firestore transaction; deny reserved words | A data-integrity invariant that is also a tenant-isolation (security) invariant, on a database with no unique constraints. |
| [0012](0012-terraform-for-infra.md) | 2026-05-21 | Terraform for project-level infrastructure, with an explicit list of what it does not own | Reliability through reproducible environments, and a deliberate boundary that avoids fighting the deploy pipeline. |

Format: Context, Decision, Alternatives considered, Consequences. Superseded decisions stay in the log with a pointer to their replacement.
