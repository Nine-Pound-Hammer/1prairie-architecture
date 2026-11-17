"""Stand-ins for internal helpers the ingestion sample imports.

The production versions live elsewhere in the private codebase. These keep
the sample self-contained and show each helper's contract.
"""
import os
import re

_RESTAURANT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def project_id_or_default() -> str:
    """Resolve the GCP project from the environment (set per Cloud Run
    service by the deploy pipeline). Placeholder default for local reading."""
    return os.environ.get("GOOGLE_CLOUD_PROJECT", "example-dev")


def is_valid_restaurant_id(value: object) -> bool:
    """A single Firestore document-id segment: no slashes, bounded length.
    Rejects values that would interpolate into a deeper path."""
    return isinstance(value, str) and bool(_RESTAURANT_ID_RE.match(value))


def check_rate_limits(db, uid: str, restaurant_id: str) -> None:
    """Sliding-window limits on AI calls: a per-user burst window, a per-user
    monthly window and a per-restaurant monthly window, backed by
    TTL-expired event documents. Raises HTTPException(429) when a window
    is full."""
    return None
