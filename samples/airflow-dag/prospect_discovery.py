"""Prospect discovery DAG (Airflow 3, TaskFlow API).

Adapted from the production pipeline. The pipeline finds independent
restaurants in a ZIP code, resolves duplicates across sources, loads them into
BigQuery idempotently, and enriches them with Gemini on Vertex AI.

Manual runs only (schedule=None). Two ways to feed it work:

  1. Manual  — trigger with conf {"zip": "<zip>"} to enqueue that ZIP first;
               omit it to just work through the queue.
  2. Pub/Sub — each run drains a subscription, so the internal admin app (or
               anything else) can stage ZIPs without touching Airflow. Staged
               ZIPs are picked up the next time the DAG runs.

Flow:  preflight → [drain_zip_requests, enqueue_manual_zip] → claim_batch → discover → enrich

Guardrails:
  - preflight logs the target GCP project and fails fast if a required
    BigQuery table is missing, instead of an opaque NotFound mid-run
  - per-ZIP result cap, applied inside connector pagination
  - per-task execution_timeout
  - a pool plus max_active_runs=1, so runs are strictly serial
  - bounded retries with exponential backoff
  - every write is an idempotent MERGE, so a retried task never duplicates rows

The DAG file is deliberately thin: the real logic lives in a unit-tested
package. In this sample that package is replaced by `pipeline_stubs.py`, which
keeps the contracts and leaves out the data sources and enrichment details.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import pendulum
from airflow.sdk import Param, dag, get_current_context, task

import pipeline_stubs as lib

log = logging.getLogger(__name__)

_DEFAULT_ARGS = {
    "retries": lib.TASK_RETRIES,
    "retry_delay": timedelta(seconds=lib.TASK_RETRY_DELAY_S),
    "retry_exponential_backoff": True,
    "execution_timeout": timedelta(seconds=lib.TASK_EXECUTION_TIMEOUT_S),
    "pool": lib.POOL_NAME,
}


def _bq_client():
    # Imported lazily to keep google-cloud-bigquery out of DAG parse time.
    # Credentials come from Application Default Credentials.
    from google.cloud import bigquery

    return bigquery.Client(project=lib.GCP_PROJECT)


@dag(
    dag_id="prospect_discovery",
    schedule=None,  # manual runs only
    start_date=pendulum.datetime(2026, 6, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,  # belt and braces with the pool: never two runs at once
    default_args=_DEFAULT_ARGS,
    tags=["prospecting"],
    params={
        "zip": Param(default="", type="string", description="Optional ZIP to enqueue before this run."),
        "max_prospects": Param(
            default=lib.MAX_PROSPECTS_PER_ZIP_DEFAULT,
            type="integer",
            minimum=1,
            maximum=lib.MAX_PROSPECTS_PER_ZIP_CEILING,
            description="Per-ZIP discovery cap (the ceiling is also enforced in code).",
        ),
    },
    doc_md=__doc__,
)
def prospect_discovery():
    @task
    def preflight() -> str:
        """Log which GCP project this run targets and verify the BigQuery
        tables it needs exist, so a misconfigured environment fails here with
        a clear message rather than halfway through discover()."""
        log.info("\n".join(lib.env_banner()))
        verified = lib.verify_tables(_bq_client())
        log.info("preflight OK — %d required tables present in %s", len(verified), lib.GCP_PROJECT)
        return lib.GCP_PROJECT

    @task
    def drain_zip_requests() -> int:
        """Pull the Pub/Sub subscription and enqueue any published ZIPs."""
        try:
            from google.api_core import exceptions as api_exceptions
            from google.cloud import pubsub_v1
        except ImportError:
            log.info("google-cloud-pubsub not installed; skipping Pub/Sub drain")
            return 0
        # The client owns a gRPC channel and background threads, and the
        # worker process is long-lived, so close it every run.
        with pubsub_v1.SubscriberClient() as subscriber:
            sub_path = subscriber.subscription_path(lib.GCP_PROJECT, lib.PUBSUB_SUBSCRIPTION)
            try:
                # A bounded synchronous pull, not a long poll: with a short
                # deadline and retry=None, an empty subscription returns
                # quickly instead of holding the run open.
                response = subscriber.pull(
                    request={"subscription": sub_path, "max_messages": 100},
                    retry=None,
                    timeout=5,
                )
            except api_exceptions.DeadlineExceeded:
                return 0  # empty queue: the common case, not an error
            except Exception as exc:  # missing subscription, auth, transport
                log.warning("Pub/Sub pull failed (%s); skipping drain", exc)
                return 0
            received = response.received_messages
            if not received:
                return 0
            zips = lib.extract_zips([m.message.data for m in received])
            if zips:
                lib.enqueue_zips(_bq_client(), zips, requested_by="pubsub")
            # Ack only after the enqueue succeeded; a failure above leaves the
            # messages for redelivery.
            subscriber.acknowledge(
                request={"subscription": sub_path, "ack_ids": [m.ack_id for m in received]}
            )
            log.info("drained %d messages → %d ZIPs enqueued", len(received), len(zips))
            return len(zips)

    @task
    def enqueue_manual_zip() -> str:
        """If the run was triggered with a `zip` param, enqueue it first."""
        zip_code = (get_current_context()["params"].get("zip") or "").strip()
        if zip_code:
            lib.enqueue_zips(_bq_client(), [zip_code], requested_by="manual", priority=10)
            log.info("enqueued manual ZIP %s", zip_code)
        return zip_code

    @task
    def claim_batch() -> list[str]:
        """Claim up to MAX_ZIPS_PER_RUN pending ZIPs, marking them in progress."""
        zips = lib.claim_pending_zips(_bq_client(), limit=lib.MAX_ZIPS_PER_RUN)
        log.info("claimed %d ZIPs: %s", len(zips), zips)
        return zips

    @task
    def discover(claimed: list[str]) -> dict:
        """Discover → resolve duplicates → idempotent load, per claimed ZIP."""
        context = get_current_context()
        cap = lib.clamp_prospect_cap(context["params"].get("max_prospects"))
        run_id = context["run_id"]
        run_type = context["dag_run"].run_type
        trigger = getattr(run_type, "value", run_type)  # str or str-enum
        # The DagRun's start, not this task's: stable across task retries, so
        # a retry's MERGE doesn't overwrite the original start time.
        dag_started = context["dag_run"].start_date
        started_at = (dag_started or pendulum.now("UTC")).isoformat()

        client = _bq_client()

        # Write a "running" row first, so every prospect stamped with this
        # run_id has a run row to point at even if the task dies mid-loop.
        # record_run is a MERGE on run_id, so the final write is idempotent.
        if claimed:
            lib.record_run(
                client,
                lib.build_run_row(
                    run_id, trigger=trigger, zips=claimed, discovered=0, kept=0,
                    status="running", started_at=started_at, finished_at=None,
                ),
            )

        totals = {"zips": 0, "discovered": 0, "kept": 0, "failed": 0}
        for zip_code in claimed:
            try:
                raw = lib.discover_zip(zip_code, cap=cap)
                resolved = lib.resolve(raw)
                written = lib.upsert_prospects(client, resolved, run_id)
            except Exception as exc:
                # One bad ZIP doesn't fail the run; it's marked and reported.
                lib.mark_zip_failed(client, zip_code, str(exc))
                totals["failed"] += 1
                log.warning("ZIP %s failed: %s", zip_code, exc)
                continue
            lib.mark_zip_done(client, zip_code)
            totals["zips"] += 1
            totals["discovered"] += len(raw)
            totals["kept"] += written  # rows sent to MERGE after cross-source dedup
            log.info("ZIP %s: %d raw → %d resolved prospects loaded", zip_code, len(raw), written)

        # "done", "partial" or "failed": a run where some ZIPs errored must
        # not read as clean in the runs table.
        status = lib.derive_run_status(totals)

        # Known gap, tracked: if this final write itself fails and retries
        # run out, the row stays "running" although every prospect loaded.
        # A reconciliation sweep for stale "running" rows is planned work.
        if claimed:
            lib.record_run(
                client,
                lib.build_run_row(
                    run_id, trigger=trigger, zips=claimed,
                    discovered=totals["discovered"], kept=totals["kept"], status=status,
                    started_at=started_at, finished_at=pendulum.now("UTC").isoformat(),
                ),
            )
        log.info("run summary: %s", totals)
        return totals

    @task(execution_timeout=timedelta(seconds=lib.ENRICH_EXECUTION_TIMEOUT_S))
    def enrich(_discover_totals: dict) -> dict:
        """Enrich up to MAX_ENRICH_PER_RUN un-enriched prospects with Gemini.

        Runs after discover so this run's new rows are enrichable, and also
        drains any backlog (enriched_at IS NULL), oldest first. A failure on
        one prospect is logged and skipped; the row stays NULL and is retried
        next run, so one bad website never fails the task.
        """
        client = _bq_client()
        rows = lib.fetch_unenriched(client, limit=lib.MAX_ENRICH_PER_RUN)
        if not rows:
            log.info("enrich: no un-enriched prospects")
            return {"enriched": 0, "all_null": 0, "est_cost_usd": 0.0}
        enricher = lib.GeminiEnricher()
        limiter = lib.RateLimiter(lib.WEBSITE_MIN_INTERVAL_S)  # politeness per website
        done = 0
        empty = 0  # enriched, but the model could determine no fields
        for row in rows:
            try:
                result = lib.enrich_row(row, model_call=enricher.call, limiter=limiter)
                lib.upsert_enrichment(client, row["id"], result)
            except Exception as exc:
                log.warning("enrich failed for %s: %s", row.get("id"), exc)
                continue
            done += 1
            if lib.enrichment_is_empty(result):
                empty += 1
        est = lib.estimate_cost_usd(done)
        # The estimated model cost is logged per run, so spend is visible
        # next to throughput.
        log.info("enrich summary: %d enriched (%d all-null), est $%.4f", done, empty, est)
        return {"enriched": done, "all_null": empty, "est_cost_usd": est}

    ready = preflight()
    drained = drain_zip_requests()
    manual = enqueue_manual_zip()
    claimed = claim_batch()
    # preflight gates everything; both enqueue paths finish before the claim.
    ready >> [drained, manual] >> claimed
    # With the default all_success trigger rule, a discover failure skips
    # enrich for that run. Intentional: the backlog drain is idempotent, so the
    # next healthy run catches up and nothing is lost.
    enrich(discover(claimed))


prospect_discovery()
