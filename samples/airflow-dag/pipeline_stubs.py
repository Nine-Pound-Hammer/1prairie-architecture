"""Stand-ins for the pipeline package the DAG imports.

In the private codebase each of these is a tested function in its own module
(config, queue, sources, resolve, load, enrich). Here they keep their
signatures and contracts. Data sources, matching rules and enrichment fields
are deliberately left out.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable

# ── Configuration (environment-driven; placeholders here) ────────────────────

GCP_PROJECT = os.environ.get("GCP_PROJECT", "example-dev")
PUBSUB_SUBSCRIPTION = os.environ.get("PUBSUB_SUBSCRIPTION", "zip-requests-sub")
POOL_NAME = "prospecting"

TASK_RETRIES = 2
TASK_RETRY_DELAY_S = 60
TASK_EXECUTION_TIMEOUT_S = 30 * 60
ENRICH_EXECUTION_TIMEOUT_S = 60 * 60

MAX_ZIPS_PER_RUN = 5
MAX_PROSPECTS_PER_ZIP_DEFAULT = 100
MAX_PROSPECTS_PER_ZIP_CEILING = 500
MAX_ENRICH_PER_RUN = 200
WEBSITE_MIN_INTERVAL_S = 2.0

Row = dict[str, Any]


def clamp_prospect_cap(value: int | None) -> int:
    """Clamp a user-supplied cap to [1, ceiling]; None means the default."""
    if value is None:
        return MAX_PROSPECTS_PER_ZIP_DEFAULT
    return max(1, min(int(value), MAX_PROSPECTS_PER_ZIP_CEILING))


# ── Preflight ────────────────────────────────────────────────────────────────


def env_banner() -> list[str]:
    """Lines stating the environment and GCP project this run will touch."""
    return [f"environment project: {GCP_PROJECT}"]


def verify_tables(client) -> list[str]:
    """Raise with an actionable message if a required table is missing;
    return the verified table names."""
    raise NotImplementedError


# ── Work queue (a BigQuery table; claims and updates are MERGE statements) ───


def extract_zips(bodies: list[bytes]) -> list[str]:
    """Parse and validate ZIPs from Pub/Sub message bodies; drop malformed ones."""
    raise NotImplementedError


def enqueue_zips(client, zips: list[str], requested_by: str, priority: int = 0) -> None:
    """Insert ZIPs as pending; a ZIP already pending is not duplicated."""
    raise NotImplementedError


def claim_pending_zips(client, limit: int) -> list[str]:
    """Mark up to `limit` pending ZIPs in progress (highest priority, oldest first)."""
    raise NotImplementedError


def mark_zip_done(client, zip_code: str) -> None:
    raise NotImplementedError


def mark_zip_failed(client, zip_code: str, error: str) -> None:
    raise NotImplementedError


# ── Discovery, resolution and load ───────────────────────────────────────────


def discover_zip(zip_code: str, cap: int) -> list[Row]:
    """Query the configured source connectors for one ZIP, paginating up to
    `cap` results per source. Each connector rate-limits itself."""
    raise NotImplementedError


def resolve(raw: list[Row]) -> list[Row]:
    """Normalize names and addresses and merge records that describe the same
    business across sources. Returns one row per resolved business."""
    raise NotImplementedError


def upsert_prospects(client, rows: list[Row], run_id: str) -> int:
    """MERGE rows into the prospects table keyed on a stable id, stamping
    last_run_id. Returns the number of rows sent."""
    raise NotImplementedError


def build_run_row(run_id: str, **fields: Any) -> Row:
    return {"run_id": run_id, **fields}


def record_run(client, row: Row) -> None:
    """MERGE the run row on run_id (idempotent across task retries)."""
    raise NotImplementedError


def derive_run_status(totals: dict[str, int]) -> str:
    """"failed" if nothing loaded and something failed, "partial" if both
    happened, otherwise "done"."""
    if totals["failed"] and not totals["zips"]:
        return "failed"
    if totals["failed"]:
        return "partial"
    return "done"


# ── Enrichment (Gemini on Vertex AI) ─────────────────────────────────────────


class RateLimiter:
    """Minimum interval between calls; one instance per task run."""

    def __init__(self, min_interval_s: float) -> None:
        self._min_interval_s = min_interval_s
        self._last = 0.0

    def wait(self) -> None:
        delay = self._last + self._min_interval_s - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()


class GeminiEnricher:
    """Wraps a Vertex AI client (Application Default Credentials) and a
    structured-output schema; `call` returns the validated result."""

    def call(self, prompt: str, content: str) -> Row:
        raise NotImplementedError


def fetch_unenriched(client, limit: int) -> list[Row]:
    """Oldest-first rows where enriched_at IS NULL."""
    raise NotImplementedError


def enrich_row(row: Row, model_call: Callable[[str, str], Row], limiter: RateLimiter) -> Row:
    raise NotImplementedError


def upsert_enrichment(client, prospect_id: str, result: Row) -> None:
    """MERGE the enrichment fields and stamp enriched_at."""
    raise NotImplementedError


def enrichment_is_empty(result: Row) -> bool:
    return all(v is None for v in result.values())


def estimate_cost_usd(calls: int) -> float:
    """Token-based estimate from the model's published pricing."""
    raise NotImplementedError
