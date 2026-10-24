"""
pipeline.py  --  Ingest ONE uploaded PDF, safely, for the right tenant.

PLACE AT: src/documind/ingestion/pipeline.py   (new file)

This is the single reusable "unit of work" for ingestion. Today you call it from
scripts/test_ingestion.py; next, the Cloud Run worker calls it once per Pub/Sub
message; the bulk/backfill script will call it in a loop. Same code path everywhere.

FLOW for object  gs://<bucket>/<tenant>/<file>.pdf
  1. Derive the tenant from the object path; reject bad paths / non-PDFs /
     unknown or suspended tenants (nothing is written for these).
  2. Look up the object in GCS. Gone? -> remove it from the index.
  3. CLAIM this version of the document (idempotency + lease). Duplicate,
     stale, or already-owned versions stop here.
  4. Validate (size, PDF magic bytes) -> parse (Document AI) -> chunk -> embed.
  5. FINALIZE in one transaction (status 'ready' + chunks swapped in).
  6. Errors: permanent -> mark 'failed'; anything else -> release the claim and
     ask for a retry. A hard attempt cap guarantees we never retry forever.

The function RETURNS an IngestOutcome instead of raising for expected situations,
so the caller (worker) can map it to an HTTP status:
    should_retry == True  -> return 5xx so Pub/Sub redelivers
    otherwise             -> return 2xx (ack)
"""

# time.monotonic() = a clock that only moves forward; ideal for measuring durations.
import time

# dataclass for the immutable result object; lru_cache to reuse one GCS client.
from dataclasses import dataclass
from functools import lru_cache

# PurePosixPath parses "acme/reports/q1.pdf" style object names reliably,
# regardless of the OS we're running on (GCS names always use "/").
from pathlib import PurePosixPath

# Literal restricts the outcome status to a known set of strings.
from typing import Literal

# Structured logging, consistent with the rest of the project.
import structlog

# google.api_core.exceptions: the typed errors Google client libraries raise
# (e.g. InvalidArgument for a corrupt PDF, ResourceExhausted for HTTP 429).
from google.api_core import exceptions as gexc

# Cloud Storage client: used to read object metadata and the first bytes.
from google.cloud import storage

# Tenant-id rule shared with auth.py and the DB CHECK constraint.
from documind.auth import is_valid_tenant_id

# Typed settings (lease length, attempt cap, size cap).
from documind.config import get_settings

# Tenant-scoped database operations (see db.py).
from documind.db import (
    StaleClaimError,
    claim_document,
    delete_document,
    finalize_document,
    get_tenant,
    mark_failed,
    release_claim,
)

# Existing pipeline steps (unchanged): chunk text, embed chunks, parse via Document AI.
from documind.ingestion.chunker import chunk_text
from documind.ingestion.embedder import embed_chunks
from documind.ingestion.parser import parse_document

logger = structlog.get_logger()


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #

IngestStatus = Literal[
    "ingested",          # success: document is 'ready'
    "duplicate",         # this version was already ingested; nothing to do
    "stale",             # a newer version exists/superseded us; ignore
    "failed_previously", # earlier permanent failure for this version; needs manual replay
    "rejected",          # bad path / not a PDF / unknown or suspended tenant (nothing stored)
    "object_gone",       # object no longer exists in the bucket; index entry removed
    "failed_permanent",  # this run failed permanently and was recorded as 'failed'
    "in_progress",       # another worker currently owns it  -> retry later
    "retry_later",       # transient error; claim released   -> retry later
]


@dataclass(frozen=True)
class IngestOutcome:
    """What happened to one ingestion attempt."""
    status: IngestStatus
    gcs_uri: str
    tenant_id: str | None = None
    document_id: str | None = None
    chunk_count: int = 0
    detail: str = ""

    @property
    def should_retry(self) -> bool:
        """True when the caller should NOT ack the message (so it is redelivered)."""
        return self.status in ("in_progress", "retry_later")


class PermanentIngestError(Exception):
    """A problem retrying cannot fix (not a PDF, too large, no text, ...)."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

@lru_cache
def _storage_client() -> storage.Client:
    """One shared GCS client per process (creating clients is relatively slow)."""
    return storage.Client()


def _split_object_name(object_name: str) -> tuple[str, str] | None:
    """Split 'acme/sub/file.pdf' into ('acme', 'file.pdf').

    The FIRST path segment is the tenant. Returns None if there is no tenant
    folder, the name ends with '/', or the tenant id breaks our naming rule.
    """
    parts = object_name.split("/", 1)          # ["acme", "sub/file.pdf"]
    if len(parts) != 2 or not parts[1] or object_name.endswith("/"):
        return None                            # file sits in the bucket root, or is a "folder" marker
    tenant_id = parts[0]
    if not is_valid_tenant_id(tenant_id):
        return None                            # e.g. uppercase, spaces, "..", too long
    return tenant_id, PurePosixPath(object_name).name  # (tenant, bare filename)


def _validate_pdf(blob: storage.Blob) -> None:
    """Cheap checks BEFORE spending money on Document AI / embeddings."""
    max_bytes = get_settings().ingest_max_pdf_mb * 1024 * 1024
    size = blob.size or 0
    if size == 0:
        raise PermanentIngestError("file is empty")
    if size > max_bytes:
        raise PermanentIngestError(f"file is {size} bytes; limit is {max_bytes}")
    # Read only the first KB and look for the PDF signature, so a renamed
    # .exe/.zip/.html cannot pass just by ending in ".pdf".
    head = blob.download_as_bytes(start=0, end=1023)
    if b"%PDF-" not in head:
        raise PermanentIngestError("file does not look like a PDF (missing %PDF- header)")


# --------------------------------------------------------------------------- #
# The unit of work
# --------------------------------------------------------------------------- #

def ingest_object(bucket_name: str, object_name: str) -> IngestOutcome:
    """Ingest (or re-ingest, or remove) one GCS object. See module docstring."""
    settings = get_settings()
    started = time.monotonic()
    gcs_uri = f"gs://{bucket_name}/{object_name}"
    log = logger.bind(gcs_uri=gcs_uri)  # every log line below automatically carries the uri

    def finish(status: IngestStatus, **fields) -> IngestOutcome:
        """Build the outcome and log it with the elapsed time (one exit path = one log line)."""
        outcome = IngestOutcome(status=status, gcs_uri=gcs_uri, **fields)
        log.info(
            "ingest_finished",
            status=status,
            tenant_id=outcome.tenant_id,
            chunks=outcome.chunk_count,
            detail=outcome.detail,
            elapsed_s=round(time.monotonic() - started, 2),
        )
        return outcome

    # ---- 1. Who owns this object? (tenant comes from the PATH, never from user input) ----
    split = _split_object_name(object_name)
    if split is None:
        return finish("rejected", detail="path must look like <tenant>/<file>.pdf with a valid tenant id")
    tenant_id, filename = split
    log = log.bind(tenant_id=tenant_id)

    if not object_name.lower().endswith(".pdf"):
        return finish("rejected", tenant_id=tenant_id, detail="only .pdf files are accepted")

    tenant = get_tenant(tenant_id)
    if tenant is None:
        return finish("rejected", tenant_id=tenant_id, detail="unknown tenant")
    if tenant.status != "active":
        return finish("rejected", tenant_id=tenant_id, detail=f"tenant is {tenant.status}")

    # ---- 2. Does the object still exist? ----
    blob = _storage_client().bucket(bucket_name).get_blob(object_name)  # None if missing
    if blob is None:
        removed = delete_document(tenant_id, gcs_uri)
        return finish("object_gone", tenant_id=tenant_id, detail=f"removed_from_index={removed}")

    # GCS gives every overwrite a new generation number -> our version id.
    generation = int(blob.generation)
    # GCS computes an MD5 for free; store it for audit / future "unchanged file" shortcuts.
    content_hash = blob.md5_hash or blob.crc32c

    # ---- 3. Claim this version ----
    claim = claim_document(
        tenant_id=tenant_id,
        gcs_uri=gcs_uri,
        original_filename=filename,
        generation=generation,
        lease_seconds=settings.ingest_lease_seconds,
    )
    if claim.outcome == "duplicate_ready":
        return finish("duplicate", tenant_id=tenant_id)
    if claim.outcome == "stale":
        return finish("stale", tenant_id=tenant_id)
    if claim.outcome == "failed_previously":
        return finish("failed_previously", tenant_id=tenant_id)
    if claim.outcome == "in_progress":
        return finish("in_progress", tenant_id=tenant_id)

    # outcome == "claimed": from here on WE own the document and must end it
    # in exactly one of: finalize, mark_failed, or release_claim.
    document_id = claim.document_id
    assert document_id is not None  # a successful claim always carries an id

    # Poison-pill guard: stop retrying a document that keeps failing.
    if claim.attempts > settings.ingest_max_attempts:
        message = f"exceeded {settings.ingest_max_attempts} attempts; last error: {claim.last_error}"
        mark_failed(tenant_id, document_id, generation, message)
        return finish("failed_permanent", tenant_id=tenant_id, document_id=document_id, detail=message)

    # ---- 4 + 5. Do the work, then finalize ----
    try:
        _validate_pdf(blob)

        text, page_count = parse_document(gcs_uri)       # Document AI OCR/parse
        if not text.strip():
            raise PermanentIngestError("no extractable text")

        chunks = chunk_text(text)                        # overlapping retrieval-sized pieces
        if not chunks:
            raise PermanentIngestError("chunker produced no chunks")

        embedded = embed_chunks(chunks)                  # Vertex AI embeddings (may hit 429)

        chunk_count = finalize_document(
            tenant_id=tenant_id,
            document_id=document_id,
            generation=generation,
            page_count=page_count,
            content_hash=content_hash,
            embedded_chunks=embedded,
        )
        return finish("ingested", tenant_id=tenant_id, document_id=document_id, chunk_count=chunk_count)

    except StaleClaimError as exc:
        # A newer upload/delete superseded us mid-flight; its own message will handle it.
        return finish("stale", tenant_id=tenant_id, document_id=document_id, detail=str(exc))

    except (PermanentIngestError, gexc.InvalidArgument) as exc:
        # Retrying cannot help (bad/corrupt file) -> record it and stop.
        mark_failed(tenant_id, document_id, generation, f"{type(exc).__name__}: {exc}")
        return finish("failed_permanent", tenant_id=tenant_id, document_id=document_id, detail=str(exc))

    except Exception as exc:  # noqa: BLE001 - deliberately broad: unknown = assume transient
        # 429s, timeouts, DB blips, ... -> free the claim and let Pub/Sub redeliver.
        # The attempt cap above guarantees this cannot loop forever.
        release_claim(tenant_id, document_id, generation, f"{type(exc).__name__}: {exc}")
        return finish("retry_later", tenant_id=tenant_id, document_id=document_id, detail=f"{type(exc).__name__}: {exc}")
