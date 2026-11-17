# Airflow prospect discovery DAG

An Airflow 3 pipeline (TaskFlow API) that finds independent restaurants by ZIP code. It loads them into BigQuery and enriches them with Gemini on Vertex AI, building the sales pipeline for the product. It's adapted from the production DAG.

## What it shows

- **A thin DAG over a tested library.** The DAG file only orchestrates. Queueing, discovery, deduplication, loading and enrichment are plain functions with unit tests. Here they are replaced by `pipeline_stubs.py`, which keeps their contracts.
- **Two ways to supply work.** A manual trigger with a parameter, or a Pub/Sub subscription the internal admin app publishes to. The subscription is drained with a bounded pull, and messages are acknowledged only after they're enqueued.
- **A work queue in BigQuery.** ZIPs move from pending to in progress to done or failed through MERGE statements, so a failed ZIP is visible and can be retried.
- **Idempotency throughout.** Prospects and run records are MERGEd on stable keys. A run row is written as "running" before any prospect points at it. Run start time comes from the DagRun, not the task, so a retried task never overwrites it.
- **Guardrails.** A preflight task names the target project and fails fast if a BigQuery table is missing. Every task has a timeout. Retries are bounded with exponential backoff. A pool plus `max_active_runs=1` keeps runs strictly serial. Per-ZIP result caps are enforced in code as well as in the parameter schema.
- **Partial failure handled honestly.** One bad ZIP or one bad website is logged and skipped rather than failing the run. The run's final status distinguishes done, partial and failed. The one known gap is written down in the code.
- **AI cost visible per run.** The enrichment step logs an estimated model cost alongside its throughput.

## What's left out

The data sources and their connectors, the duplicate-matching rules, the enrichment fields and the BigQuery schema. Those are business-specific.

The files are syntactically valid Python 3.12 (`python -m py_compile *.py`). They target Airflow 3 (`airflow.sdk`).
