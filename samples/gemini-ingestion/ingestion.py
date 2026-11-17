"""Menu ingestion via Vertex AI (Gemini): document in, structured draft out.

Excerpted and adapted from the production API service (FastAPI on Cloud Run).
The flow:

    owner uploads PDF/photos to Cloud Storage (client SDK, per-user path)
      -> POST /ingest with the storage paths
      -> authorize: the caller owns the restaurant AND the storage paths
      -> validate size/type, check rate limits, count the attempt per tenant
      -> one Gemini call with all pages, response constrained to a JSON schema
      -> Pydantic validation of the response
      -> pending draft in Firestore for the owner to review
      -> accept: one atomic batch writes sections + items (append or replace)

Authentication is Application Default Credentials: on Cloud Run the service's
runtime service account, locally `gcloud auth application-default login`.
There is no API key anywhere in this path.

Internal helpers that are not the point of this sample live in `_stubs.py`.
"""
import json
import logging
import os
import random
import re
import time
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from firebase_admin import firestore
from pydantic import BaseModel

from _stubs import check_rate_limits, is_valid_restaurant_id, project_id_or_default

# Storage paths produced by the web client's upload helper:
#
#   restaurants/{restaurantId}/ingestion/{uid}/{filename}                 (single file)
#   restaurants/{restaurantId}/ingestion/{uid}/{batchId}/{n}-{filename}   (multi-page batch)
#
# Storage security rules only enforce the {uid} segment (the caller must own
# the path), so the API double-checks that {restaurantId} matches the caller's
# actual restaurant before reading the file. Otherwise a user could upload to
# their own /ingestion/{uid}/ subtree under any restaurantId in the URL.
_STORAGE_PATH_RE = re.compile(
    r"^restaurants/(?P<restaurant_id>[^/]+)/ingestion/(?P<uid>[^/]+)/(?P<rest>[^/].*)$"
)

PROJECT_ID = project_id_or_default()
LOCATION = os.environ["VERTEX_AI_LOCATION"]  # set per environment by the deploy
# The model is configuration, not code: an env var overrides it per
# environment, so a model retirement or a trial of a newer model is a deploy
# setting, and a rollback doesn't need a code change.
MODEL = os.environ.get("VERTEX_AI_MODEL", "gemini-3.6-flash")

# MIME types Gemini multimodal accepts that we expose as menu uploads. The
# frontend validates against the same list so the two never drift.
ACCEPTED_MIME_TYPES: tuple[str, ...] = (
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # per page
MAX_PAGES_PER_IMPORT = 10
# Below Gemini's inline-data ceiling, so the user gets a clear 413 *before*
# the model rejects the call with a less helpful error.
MAX_TOTAL_BYTES_PER_IMPORT = 50 * 1024 * 1024

_logger = logging.getLogger(__name__)

_PROMPT = """You are a menu parser. Extract the full menu from the attached document(s).
The document may span multiple pages provided in order — treat them as one menu, not separate menus.
Return ONLY valid JSON matching the schema — no commentary, no markdown fences.

Rules:
- Preserve section names exactly as written (e.g. "Appetizers", "Entrées")
- If the same section header appears on multiple pages (e.g. "Appetizers" repeats on page 2 because the section continued), treat it as ONE section and merge its items in document order — do not emit duplicate sections
- Prices must be strings exactly as printed (e.g. "$12.50", "12.50", "MKT")
- Use empty string for price or description when not visible in the document
- Do not invent or infer content not present in the document
- If there are no clear sections, use a single section with an empty string name
- Preserve the order in which items appear across the pages
"""


# ── Pydantic models (also passed to Vertex AI as the response schema) ────────


class ParsedMenuItem(BaseModel):
    name: str
    description: str = ""
    price: str = ""


class ParsedMenuSection(BaseModel):
    name: str
    items: list[ParsedMenuItem]


class ParsedMenu(BaseModel):
    sections: list[ParsedMenuSection]


# ── Transient Vertex AI error handling ───────────────────────────────────────
#
# Gemini's dynamic shared quota can return 429 RESOURCE_EXHAUSTED (capacity
# throttling, not our provisioned quota) or 503 UNAVAILABLE under load. A
# retry-free call turns a momentary blip into a user-facing failure (this
# happened once in production). We retry with bounded exponential backoff and
# jitter; only when retries are exhausted do we surface a user-visible
# "AI is busy" 429, which has its own alert (see the terraform-monitoring
# sample).
#
# Bound: attempts x ~30s model latency + backoff must stay under the Cloud Run
# request timeout (300s). 3 attempts is about 90s plus ~6s of backoff.
_AI_MAX_ATTEMPTS = 3
_AI_BACKOFF_BASE_SECONDS = 2.0
_AI_RETRY_AFTER_SECONDS = 30
# 429 = capacity, 503 = transient unavailability. Everything else (4xx bad
# input, 500 model error) is terminal: retrying won't help.
_TRANSIENT_AI_STATUS = frozenset({429, 503})


class AiCapacityError(Exception):
    """Vertex AI stayed out of capacity after our retries. The caller maps
    this to HTTP 429 + Retry-After so the client shows "try again in a
    moment" instead of a generic error. Terminal parse failures stay 502."""

    retry_after_seconds = _AI_RETRY_AFTER_SECONDS


def _is_transient_ai_error(exc: Exception) -> bool:
    """True if `exc` is a retryable capacity/availability blip. Prefers the
    SDK's numeric `.code`; falls back to the status names the SDK embeds in
    the message, so a client-library shape change can't silently disable
    retries."""
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code in _TRANSIENT_AI_STATUS:
        return True
    text = str(exc).upper()
    return "RESOURCE_EXHAUSTED" in text or "UNAVAILABLE" in text


def _model_tuning(types, model: str) -> dict:
    """Gemini 3.x-only config; empty for 2.x.

    Transcribing a menu into a schema is extraction, not reasoning, so
    thinking is held at LOW: thinking tokens bill as output, and 3.x Flash
    defaults to MEDIUM. MEDIUM media resolution is Google's recommended
    setting for documents (OCR quality saturates there). Gated on the model
    family because 2.x rejects `thinking_level` and the env var can still
    point back at 2.x as a rollback.
    """
    if model.startswith("gemini-2"):
        return {}
    return {
        "thinking_config": types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
        "media_resolution": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
    }


def parse_menu_from_bytes(parts: list[tuple[bytes, str]]) -> ParsedMenu:
    """Extract a structured menu from one or more pages, each a
    `(bytes, mime_type)` tuple in document order, in a single model call.

    The response is constrained to the ParsedMenu JSON schema at the API
    level and then validated again by Pydantic, so a malformed response
    fails here rather than being written to Firestore.
    """
    if not parts:
        raise ValueError("parse_menu_from_bytes requires at least one page")

    from google import genai
    from google.genai import types

    client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)
    file_parts = [types.Part.from_bytes(data=data, mime_type=mime) for data, mime in parts]
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=ParsedMenu,
        **_model_tuning(types, MODEL),
    )

    last_exc: Exception | None = None
    for attempt in range(1, _AI_MAX_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=[*file_parts, _PROMPT],
                config=config,
            )
            return ParsedMenu.model_validate_json(response.text)
        except Exception as exc:  # classify first, then retry or re-raise
            if not _is_transient_ai_error(exc):
                raise
            last_exc = exc
            if attempt < _AI_MAX_ATTEMPTS:
                backoff = _AI_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
                time.sleep(backoff + random.uniform(0, backoff / 2))

    raise AiCapacityError(
        f"Vertex AI capacity exhausted after {_AI_MAX_ATTEMPTS} attempts: {last_exc}"
    )


# ── Business logic ───────────────────────────────────────────────────────────


class IngestionPage(BaseModel):
    """One source page within a multi-page menu import."""

    storage_path: str
    mime_type: str = "application/pdf"


def start_ingestion_for_uid(uid: str, pages: list[IngestionPage]) -> dict:
    """Parse already-uploaded pages into one pending draft.

    Order matters: cheap validation first, then authorization, then size
    checks on the downloaded bytes, then rate limits, then the per-tenant
    usage counter, and only then the paid model call.
    """
    from firebase_admin import storage as fb_storage

    if not pages:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "At least one page is required.")
    if len(pages) > MAX_PAGES_PER_IMPORT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Too many pages ({len(pages)}). Max {MAX_PAGES_PER_IMPORT} per import.",
        )
    for page in pages:
        if page.mime_type not in ACCEPTED_MIME_TYPES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Unsupported file type '{page.mime_type}'. "
                f"Allowed: {', '.join(ACCEPTED_MIME_TYPES)}.",
            )

    db = firestore.client()
    restaurant_id = _resolve_restaurant_id(db, uid)
    for page in pages:
        _assert_storage_path_belongs_to_caller(page.storage_path, restaurant_id, uid)

    # Download everything up front so one oversize page short-circuits
    # before we pay for the model call. Order is preserved.
    page_bytes: list[tuple[bytes, str]] = []
    total_bytes = 0
    try:
        bucket = fb_storage.bucket()
        for page in pages:
            data = bucket.blob(page.storage_path).download_as_bytes()
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    f"Page is {len(data) // (1024 * 1024)} MB; the per-page limit is "
                    f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB. Try a lower-resolution "
                    "photo or split your PDF.",
                )
            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_BYTES_PER_IMPORT:
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    f"Combined pages exceed {MAX_TOTAL_BYTES_PER_IMPORT // (1024 * 1024)} MB. "
                    "Try fewer pages or smaller photos.",
                )
            page_bytes.append((data, page.mime_type))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Could not read file from storage: {exc}")

    # After validation (bad requests don't burn quota), before the model call
    # (the attempt counts even if the draft write fails afterwards).
    check_rate_limits(db, uid, restaurant_id)
    _increment_ai_usage(db, restaurant_id)  # once per import, not per page

    first = pages[0]
    try:
        parsed_menu = parse_menu_from_bytes(page_bytes)
    except AiCapacityError as exc:
        _log_ingestion(restaurant_id, uid, first, len(pages), success=False, error=str(exc))
        # A stable key that a log-based alert counts: Vertex was out of
        # capacity AFTER retries, so a real user was turned away.
        _logger.warning(
            "event=ai_capacity_exhausted ingest turned user away: "
            f"restaurant={restaurant_id} {exc}"
        )
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "The menu reader is busy right now. Please try again in a moment.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    except Exception as exc:
        _log_ingestion(restaurant_id, uid, first, len(pages), success=False, error=str(exc))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"AI parsing failed: {exc}")

    draft_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    db.document(f"restaurants/{restaurant_id}/ingest_drafts/{draft_id}").set(
        {
            "status": "pending",
            "pages": [
                {"storagePath": p.storage_path, "mimeType": p.mime_type, "order": i}
                for i, p in enumerate(pages)
            ],
            "parsedMenu": parsed_menu.model_dump(),
            "parsedAt": now,
            "createdAt": now,
        }
    )

    _log_ingestion(restaurant_id, uid, first, len(pages), success=True)
    return {"draft_id": draft_id, "status": "pending", "restaurant_id": restaurant_id}


def accept_draft_for_uid(uid: str, draft_id: str, mode: str = "append") -> dict:
    """Promote a pending draft to live sections and items in one atomic batch.

    `append` writes new sections after the existing ones; `replace` deletes
    the existing sections and items in the same batch, so the menu is never
    observed half-swapped.
    """
    if mode not in ("append", "replace"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Invalid mode '{mode}'. Allowed: 'append', 'replace'."
        )

    db = firestore.client()
    restaurant_id = _resolve_restaurant_id(db, uid)

    draft_ref = db.document(f"restaurants/{restaurant_id}/ingest_drafts/{draft_id}")
    draft_snap = draft_ref.get()
    if not draft_snap.exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Draft not found.")
    draft = draft_snap.to_dict()
    if draft.get("status") != "pending":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Draft is not pending (status: {draft.get('status')})."
        )

    parsed_menu = draft["parsedMenu"]
    now = datetime.now(timezone.utc)
    sections = db.collection(f"restaurants/{restaurant_id}/sections")
    items = db.collection(f"restaurants/{restaurant_id}/items")
    batch = db.batch()

    if mode == "replace":
        for doc in sections.stream():
            batch.delete(doc.reference)
        for doc in items.stream():
            batch.delete(doc.reference)
        section_order_start = 0
    else:
        section_order_start = len(list(sections.order_by("order").stream()))

    for section_idx, section_data in enumerate(parsed_menu["sections"]):
        section_ref = sections.document()
        batch.set(
            section_ref,
            {
                "name": section_data["name"],
                "order": section_order_start + section_idx,
                "restaurantId": restaurant_id,
                "createdAt": now,
                "updatedAt": now,
            },
        )
        for item_idx, item_data in enumerate(section_data.get("items", [])):
            batch.set(
                items.document(),
                {
                    "name": item_data["name"],
                    "description": item_data.get("description", ""),
                    "price": item_data.get("price", ""),
                    "sectionId": section_ref.id,
                    "order": item_idx,
                    "available": True,
                    "restaurantId": restaurant_id,
                    "createdAt": now,
                    "updatedAt": now,
                },
            )

    batch.update(draft_ref, {"status": "accepted", "acceptedAt": now, "acceptMode": mode})
    batch.commit()
    return {"accepted": True, "draft_id": draft_id, "restaurant_id": restaurant_id, "mode": mode}


# ── Tenant isolation ─────────────────────────────────────────────────────────


def _resolve_restaurant_id(db, uid: str) -> str:
    """Resolve and authorize the caller's restaurant.

    Two guards beyond "is there a link":

    1. Path injection. `users/{uid}.restaurantId` is client-writeable, so a
       crafted multi-segment value would interpolate into a deeper Firestore
       path and bypass the ownership check below.
    2. Ownership. Without `ownerId == uid`, a signed-in attacker could plant
       a victim's restaurant id on their own user doc and call any ingestion
       endpoint; `accept_draft_for_uid(mode="replace")` would then wipe the
       victim's menu. A missing restaurant is folded into the same 403 so the
       endpoint doesn't reveal which ids exist.

    The warning log line carries a stable key ("cross-tenant probe") that a
    log-based alert pages on at a threshold of zero.
    """
    user_snap = db.document(f"users/{uid}").get()
    restaurant_id = (user_snap.to_dict() or {}).get("restaurantId")
    if not restaurant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No restaurant linked to this account.")

    if not is_valid_restaurant_id(restaurant_id):
        _logger.warning(
            "ingest: rejected path-injected restaurantId event=path_injection "
            "attacker_uid=%s value=%r",
            uid,
            restaurant_id,
        )
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid restaurant reference.")

    restaurant = db.document(f"restaurants/{restaurant_id}").get().to_dict() or {}
    if restaurant.get("ownerId") != uid:
        _logger.warning(
            "ingest: 403 cross-tenant probe attacker_uid=%s target_restaurant_id=%s",
            uid,
            restaurant_id,
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "You don't have permission to ingest into this restaurant."
        )
    return restaurant_id


def _assert_storage_path_belongs_to_caller(
    storage_path: str, expected_restaurant_id: str, expected_uid: str
) -> None:
    """Reject storage paths outside the caller's own ingestion subtree."""
    match = _STORAGE_PATH_RE.match(storage_path)
    if not match:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Invalid storage_path. Expected "
            "restaurants/{restaurantId}/ingestion/{uid}/<filename or batch>/<file>.",
        )
    if match.group("restaurant_id") != expected_restaurant_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "storage_path restaurant does not match your account."
        )
    if match.group("uid") != expected_uid:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "storage_path uid does not match your account.")


# ── Per-tenant AI usage and cost attribution ─────────────────────────────────


def _increment_ai_usage(db, restaurant_id: str) -> None:
    """Per-tenant usage counter on the restaurant document. Attempts include
    failures, because a failed call can still cost money."""
    db.document(f"restaurants/{restaurant_id}").set(
        {
            "aiUsage": {
                "ingestAttempts": firestore.Increment(1),
                "lastIngestAt": firestore.SERVER_TIMESTAMP,
            }
        },
        merge=True,
    )


def _log_ingestion(
    restaurant_id: str,
    uid: str,
    first_page: IngestionPage,
    page_count: int,
    success: bool,
    error: str | None = None,
) -> None:
    """One structured log line per model call. Cloud Logging indexes the
    JSON fields, so per-tenant usage (calls, pages, model, failures) is a
    query, and the same fields join against the billing export for cost."""
    payload: dict = {
        "tenant_id": uid,
        "restaurant_id": restaurant_id,
        "operation": "menu_ingest",
        "model": MODEL,
        "mime_type": first_page.mime_type,
        "page_count": page_count,
        "success": success,
    }
    if error:
        payload["error"] = error
    if success:
        _logger.info(json.dumps(payload))
    else:
        _logger.warning(json.dumps(payload))
