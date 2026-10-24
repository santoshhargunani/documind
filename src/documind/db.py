"""
db.py  --  Tenant-aware persistence layer (Cloud SQL Postgres + pgvector).

PLACE AT: src/documind/db.py   (REPLACES the existing file)

BIG IDEAS
  1. Every database call takes a tenant_id. We open a connection as the
     low-privilege role `documind_app` and tell Postgres which tenant this
     transaction belongs to with set_config('app.tenant_id', ..., true).
     Row-Level Security policies (schema.sql) then hide/forbid every row that
     belongs to anyone else. Even a buggy query cannot cross tenants.
  2. Ingestion is made safe for duplicate / retried Pub/Sub messages with a
     "claim" step: a worker atomically claims a document version (with a time
     lease) before doing expensive work, and finalises it in ONE transaction.
  3. A document only becomes `ready` in the same transaction that swaps in its
     new chunks, so queries never see a half-indexed document.

IMPORTANT RULE FOR CALLERS
  Do all work for one logical step inside ONE `with tenant_connection(...)`
  block and never call commit() yourself. The tenant setting is
  transaction-local: it disappears at commit/rollback, which is exactly what
  prevents a pooled connection from leaking one tenant's identity to another.

BREAKING CHANGE
  The old get_connection() / insert_document() / insert_chunks() /
  persist_document() are gone. Any other module that imported them (retriever,
  MCP server, API, eval) will fail loudly on import until it is made
  tenant-aware -- that is intentional (a silent un-scoped query would now just
  return zero rows).
"""

# contextmanager lets us write a `with ...:` helper as a generator function:
# code before `yield` is setup, code after is guaranteed teardown.
from contextlib import contextmanager

# dataclass = lightweight typed containers for results (no boilerplate __init__).
from dataclasses import dataclass

# Iterator types the generator in tenant_connection; Literal restricts a string
# to a fixed set of allowed values (so typos are caught by mypy).
from typing import Iterator, Literal

# pg8000: pure-Python Postgres driver (no C compiler needed on Windows).
# `.dbapi` gives the standard DB-API interface: connect(), cursor(), execute().
import pg8000.dbapi

# Structured (key=value / JSON) logging, consistent with the rest of the project.
import structlog

# Our strict tenant-id rule (same one auth.py and the database CHECK use).
from documind.auth import is_valid_tenant_id

# Typed settings: db_host, db_port, db_name, db_app_user, db_sslmode, ...
from documind.config import get_settings

# Fetches the APP role's password from Secret Manager (cached after first call).
from documind.config_secrets import get_app_db_password

# The object we persist: a Chunk (text + position) plus its 768-dim embedding.
from documind.ingestion.embedder import EmbeddedChunk

# Module-level logger.
logger = structlog.get_logger()


# --------------------------------------------------------------------------- #
# Result types and errors
# --------------------------------------------------------------------------- #

class StaleClaimError(Exception):
    """Raised when we try to finish/fail a document version that is no longer
    the current one (a newer upload superseded it, or it was deleted)."""


@dataclass(frozen=True)
class TenantInfo:
    """The few tenant facts the worker needs."""
    tenant_id: str
    status: str                # 'active' or 'suspended'
    max_concurrent_docs: int   # per-tenant concurrency cap (enforced in a later step)


# All the ways a claim attempt can end.
ClaimOutcome = Literal[
    "claimed",            # we own this document version now -> go process it
    "duplicate_ready",    # this exact version is already fully ingested -> nothing to do
    "stale",              # a NEWER version already exists -> ignore this message
    "failed_previously",  # this version failed permanently earlier -> don't retry automatically
    "in_progress",        # another worker holds a live lease -> retry later
]


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of claim_document()."""
    outcome: ClaimOutcome
    document_id: str | None = None   # set when outcome == "claimed"
    attempts: int = 0                # how many times this version has been claimed
    last_error: str | None = None    # previous failure message, if any


# --------------------------------------------------------------------------- #
# Connection management
# --------------------------------------------------------------------------- #

@contextmanager
def tenant_connection(tenant_id: str) -> Iterator[pg8000.dbapi.Connection]:
    """
    Open a connection as the low-privilege app role, scoped to ONE tenant, and
    run everything inside ONE transaction.

    Behaviour:
      * normal exit of the `with` block  -> COMMIT
      * any exception inside the block   -> ROLLBACK, then re-raise
      * always                           -> connection is closed

    Usage:
        with tenant_connection("acme") as conn:
            cur = conn.cursor()
            cur.execute("SELECT ...")
    """
    # Defense in depth: refuse malformed tenant ids before they reach the DB.
    if not is_valid_tenant_id(tenant_id):
        raise ValueError(f"invalid tenant_id: {tenant_id!r}")

    settings = get_settings()

    # Open the TCP connection as documind_app (NOT the admin), so RLS applies.
    conn = pg8000.dbapi.connect(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_app_user,
        password=get_app_db_password(),
        # Cloud SQL is configured ENCRYPTED_ONLY, so negotiate TLS when "require".
        ssl_context=True if settings.db_sslmode == "require" else None,
    )
    try:
        cursor = conn.cursor()
        # set_config(name, value, is_local):
        #   is_local = true  -> setting lasts only until the end of this transaction
        #                       (the equivalent of SET LOCAL, but it accepts a bound
        #                        parameter, which SET LOCAL does not -> no SQL injection).
        # The RLS policies read this value via current_setting('app.tenant_id').
        cursor.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
        cursor.close()
        # Hand the ready-to-use connection to the caller's `with` block.
        yield conn
        # Caller finished without error -> make all of its changes permanent.
        conn.commit()
    except Exception:
        # Something failed -> undo every change made in this transaction.
        conn.rollback()
        raise
    finally:
        # Always release the connection, success or failure.
        conn.close()


# --------------------------------------------------------------------------- #
# Tenant lookup
# --------------------------------------------------------------------------- #

def get_tenant(tenant_id: str) -> TenantInfo | None:
    """Return the tenant's info, or None if no such tenant exists.

    Works because the `tenant_self_read` policy lets a tenant context read its
    OWN row only (so this can't be used to list other tenants).
    """
    with tenant_connection(tenant_id) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status, max_concurrent_docs FROM tenants WHERE tenant_id = %s",
            (tenant_id,),
        )
        row = cursor.fetchone()  # None if the tenant row doesn't exist
    if row is None:
        return None
    return TenantInfo(tenant_id=tenant_id, status=row[0], max_concurrent_docs=row[1])


# --------------------------------------------------------------------------- #
# Ingestion lifecycle: claim -> (process elsewhere) -> finalize / fail / release
# --------------------------------------------------------------------------- #

def claim_document(
    tenant_id: str,
    gcs_uri: str,
    original_filename: str,
    generation: int,
    lease_seconds: int,
) -> ClaimResult:
    """
    Atomically try to take ownership of one version (generation) of a document.

    A single INSERT ... ON CONFLICT statement does it, so two workers racing on
    the same message cannot both win. The UPDATE branch only fires when:
      * the incoming generation is NEWER than what we have (an overwrite), or
      * it is the same generation, still 'processing', and the previous
        worker's lease has EXPIRED (that worker presumably died).
    Otherwise zero rows come back and we SELECT the row to explain why.
    """
    with tenant_connection(tenant_id) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO documents
                (tenant_id, gcs_uri, original_filename, generation,
                 status, attempts, lease_expires_at)
            VALUES
                (%s, %s, %s, %s, 'processing', 1, now() + make_interval(secs => %s))
            ON CONFLICT (tenant_id, gcs_uri) DO UPDATE SET
                original_filename = EXCLUDED.original_filename,
                generation        = EXCLUDED.generation,
                status            = 'processing',
                -- same version re-claimed -> count another attempt; new version -> start at 1
                attempts          = CASE WHEN documents.generation = EXCLUDED.generation
                                         THEN documents.attempts + 1 ELSE 1 END,
                lease_expires_at  = EXCLUDED.lease_expires_at,
                updated_at        = now()
            WHERE documents.generation < EXCLUDED.generation
               OR (documents.generation = EXCLUDED.generation
                   AND documents.status = 'processing'
                   AND (documents.lease_expires_at IS NULL
                        OR documents.lease_expires_at < now()))
            RETURNING id, attempts, last_error;
            """,
            (tenant_id, gcs_uri, original_filename, generation, float(lease_seconds)),
        )
        row = cursor.fetchone()

        if row is not None:
            # We won the claim (new row inserted, or stale/expired row taken over).
            return ClaimResult(
                outcome="claimed",
                document_id=str(row[0]),
                attempts=row[1],
                last_error=row[2],
            )

        # We did not win. Look at the existing row to classify why.
        cursor.execute(
            "SELECT generation, status FROM documents WHERE tenant_id = %s AND gcs_uri = %s",
            (tenant_id, gcs_uri),
        )
        existing = cursor.fetchone()

    if existing is None:
        # Row vanished between our INSERT and SELECT (concurrent delete). Ask for a retry.
        return ClaimResult(outcome="in_progress")

    existing_generation, existing_status = existing
    if existing_generation > generation:
        return ClaimResult(outcome="stale")              # a newer upload already exists
    if existing_status == "ready":
        return ClaimResult(outcome="duplicate_ready")    # this version is already indexed
    if existing_status == "failed":
        return ClaimResult(outcome="failed_previously")  # failed permanently; needs manual replay
    return ClaimResult(outcome="in_progress")            # another worker is on it right now


def finalize_document(
    tenant_id: str,
    document_id: str,
    generation: int,
    page_count: int | None,
    content_hash: str | None,
    embedded_chunks: list[EmbeddedChunk],
) -> int:
    """
    Mark the document 'ready' and swap in its new chunks, ALL IN ONE TRANSACTION.

    Because it is one transaction, readers see either the complete OLD version
    or the complete NEW version -- never a mixture or a half-written document.
    Raises StaleClaimError (changing nothing) if our claim is no longer current.
    Returns the number of chunks written.
    """
    with tenant_connection(tenant_id) as conn:
        cursor = conn.cursor()

        # Step 1: flip to 'ready', but ONLY if this exact generation is still
        # ours and still 'processing'. This also row-locks the document so no
        # other worker can modify it until we commit.
        cursor.execute(
            """
            UPDATE documents
               SET status = 'ready',
                   page_count = %s,
                   content_hash = %s,
                   lease_expires_at = NULL,
                   last_error = NULL,
                   ingested_at = now(),
                   updated_at = now()
             WHERE id = %s::uuid AND generation = %s AND status = 'processing'
            RETURNING id;
            """,
            (page_count, content_hash, document_id, generation),
        )
        if cursor.fetchone() is None:
            # Nothing updated -> a newer version or a delete superseded our claim.
            raise StaleClaimError(f"claim no longer current for document {document_id}")

        # Step 2: remove the chunks of any previous version of this document.
        cursor.execute(
            "DELETE FROM chunks WHERE tenant_id = %s AND document_id = %s::uuid",
            (tenant_id, document_id),
        )

        # Step 3: build one parameter tuple per chunk.
        rows = []
        for item in embedded_chunks:
            # pg8000 has no native pgvector type; pgvector accepts the text form
            # "[0.1,0.2,...]" and we cast it with ::vector in the SQL below.
            embedding_str = "[" + ",".join(str(x) for x in item.embedding) + "]"
            rows.append(
                (
                    tenant_id,
                    document_id,
                    item.chunk.chunk_index,
                    item.chunk.char_start,
                    item.chunk.char_end,
                    item.chunk.text,
                    embedding_str,
                )
            )

        # Step 4: insert all new chunks. Parameterised -> safe from SQL injection.
        cursor.executemany(
            """
            INSERT INTO chunks
                (tenant_id, document_id, chunk_index, char_start, char_end, text, embedding)
            VALUES
                (%s, %s::uuid, %s, %s, %s, %s, %s::vector);
            """,
            rows,
        )
        # No commit() here: tenant_connection commits when this block exits cleanly.

    logger.info(
        "document_finalized",
        tenant_id=tenant_id,
        document_id=document_id,
        chunk_count=len(rows),
    )
    return len(rows)


def mark_failed(tenant_id: str, document_id: str, generation: int, error: str) -> None:
    """Record a PERMANENT failure (e.g. corrupt PDF). Not retried automatically."""
    with tenant_connection(tenant_id) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE documents
               SET status = 'failed',
                   last_error = %s,
                   lease_expires_at = NULL,
                   updated_at = now()
             WHERE id = %s::uuid AND generation = %s AND status = 'processing';
            """,
            (error[:2000], document_id, generation),  # cap length so a huge trace can't bloat the row
        )
    logger.warning("document_failed", tenant_id=tenant_id, document_id=document_id, error=error[:500])


def release_claim(tenant_id: str, document_id: str, generation: int, error: str) -> None:
    """Give up our claim after a TRANSIENT error so the redelivered message can
    re-claim immediately (by expiring the lease now) instead of waiting it out."""
    with tenant_connection(tenant_id) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE documents
               SET last_error = %s,
                   lease_expires_at = now(),
                   updated_at = now()
             WHERE id = %s::uuid AND generation = %s AND status = 'processing';
            """,
            (error[:2000], document_id, generation),
        )
    logger.warning("claim_released", tenant_id=tenant_id, document_id=document_id, error=error[:500])


def delete_document(tenant_id: str, gcs_uri: str) -> bool:
    """Remove a document (its chunks go too, via ON DELETE CASCADE).
    Used when the object is deleted from the bucket. Returns True if a row existed."""
    with tenant_connection(tenant_id) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM documents WHERE tenant_id = %s AND gcs_uri = %s",
            (tenant_id, gcs_uri),
        )
        deleted = cursor.rowcount > 0  # rowcount = number of rows the DELETE removed
    if deleted:
        logger.info("document_deleted", tenant_id=tenant_id, gcs_uri=gcs_uri)
    return deleted


# --------------------------------------------------------------------------- #
# Small helper used by tests / demos
# --------------------------------------------------------------------------- #

def count_chunks(tenant_id: str) -> int:
    """How many chunks does this tenant's context see?

    NOTE there is deliberately NO `WHERE tenant_id = ...` here: the count is
    filtered purely by Row-Level Security, so it demonstrates that the database
    itself enforces the wall.
    """
    with tenant_connection(tenant_id) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM chunks")
        return int(cursor.fetchone()[0])
