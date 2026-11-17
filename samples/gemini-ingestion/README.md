# Gemini menu ingestion

A restaurant owner uploads a PDF or photos of their menu, and gets back an editable, structured menu: sections, items, descriptions, prices. This is the server side of that feature, adapted from the production FastAPI service on Cloud Run.

## What it shows

- **Structured output, validated twice.** The Pydantic model is passed to Vertex AI as the response schema, so Gemini is constrained to that JSON shape. Then the response is parsed with the same model, so a malformed answer fails before anything is written.
- **A prompt written as rules.** It covers sections that continue across pages, prices kept exactly as printed, and an instruction never to invent content. The output is a draft the owner reviews, not a direct write to the live menu.
- **Retries that fit the platform.** Only 429 and 503 are retried, with exponential backoff and jitter, and the total is bounded so it fits inside the Cloud Run request timeout. When retries run out, the user gets a 429 with `Retry-After` instead of a 502, and a log line with a stable key that a log-based alert counts.
- **Cost control in the right order.** Cheap validation runs first, then authorization, then size limits on the downloaded bytes, then rate limits, and only then the paid model call.
- **Tenant isolation in the API, not only in security rules.** The caller must own the restaurant, and the storage paths must sit inside the caller's own upload subtree. Values a client can write are checked for path injection. A failed ownership check logs a "cross-tenant probe" line that pages at a threshold of zero.
- **Per-tenant AI usage.** Each import increments a Firestore counter on the tenant's document and writes one structured log line (tenant, model, pages, success). Usage per tenant is then a log query, and the same fields join with the GCP billing export for cost.
- **An atomic accept step.** Accepting a draft writes every section and item in one Firestore batch. Replace mode deletes the old menu in the same batch, so the menu is never seen half-swapped.
- **No keys.** Vertex AI is reached with Application Default Credentials and the service's runtime service account.

## Files

- `ingestion.py`: the excerpt. Two helper routes (reject a draft, list drafts) are left out.
- `_stubs.py`: stand-ins for internal helpers (project resolution, id validation, rate limiting), showing their contracts.

The code is syntactically valid Python 3.12 (`python -m py_compile *.py`). Running it needs a GCP project with Vertex AI, Firestore and Cloud Storage. It isn't packaged to run here.
