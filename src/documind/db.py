"""
db.py
-----
Persists ingested documents and their embedded chunks into Cloud SQL
(Postgres + pgvector). This is the final step of the ingestion
pipeline: upload -> parse -> chunk -> embed -> **write to database**.

Design notes:
- Uses pg8000 (pure-Python Postgres driver, chosen back in Step 4.1 to
  avoid needing a C compiler on Windows).
- Connects over whatever DB_HOST is configured — a public IP with SSL
  for local development right now, and later a private IP with no
  public exposure at all once this code runs inside GKE (same VPC as
  the database). The connection logic itself doesn't need to change
  between those two cases, only the .env value does.
- Every write happens inside an explicit transaction, committed only
  after both the document row and all its chunk rows succeed — so a
  failure partway through never leaves a document with only some of
  its chunks persisted.
"""

# --- IMPORTS ---

from contextlib import contextmanager
# Standard library. Used to build a "context manager" — an object that
# supports Python's `with` statement, guaranteeing setup/teardown code
# (opening and closing a database connection, here) always runs, even
# if an exception occurs partway through. This is the same principle
# as `with open(file) as f:` automatically closing a file.

from typing import Iterator
# Standard library typing helper. Used to type-annotate the generator
# function get_connection() below — it "yields" a connection rather
# than returning one outright, and Iterator[Connection] is how we
# describe that to mypy.

import pg8000.dbapi
# The actual Postgres driver. pg8000.dbapi gives us a DB-API 2.0
# compliant interface (connect(), cursor(), execute(), etc.) — the
# same standard interface pattern used by psycopg2, sqlite3, and
# most other Python database drivers, so this code would look
# familiar to anyone who's used any of them.

import structlog
# Structured logging, consistent with every other module in this
# project.

from documind.config import get_settings
# Our Settings loader — gives us db_host, db_port, db_name, db_user,
# db_sslmode without hardcoding any of them here.

from documind.config_secrets import get_db_password
# Fetches the actual database password from Secret Manager at
# connection time — never stored in code, config files, or logs.

from documind.ingestion.embedder import EmbeddedChunk
# The data model we're persisting — each EmbeddedChunk carries its
# original Chunk (text, position) plus its 768-dimension embedding
# vector, produced in Step 4.7.


# --- LOGGER SETUP ---

logger = structlog.get_logger()


# --- CONNECTION MANAGEMENT ---

@contextmanager
def get_connection() -> Iterator[pg8000.dbapi.Connection]:
    """
    Opens a Postgres connection using pg8000, configured from Settings
    and Secret Manager, and guarantees it's closed afterward.

    Usage:
        with get_connection() as conn:
            ... use conn ...
    The connection is automatically closed when the `with` block exits,
    whether it exits normally or via an exception.
    """

    settings = get_settings()
    # Load cached config: db_host, db_port, db_name, db_user, db_sslmode.

    password = get_db_password()
    # Fetch the real password from Secret Manager (cached after first
    # call, per Step 4.4's @lru_cache).

    conn = pg8000.dbapi.connect(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=password,
        ssl_context=True if settings.db_sslmode == "require" else None,
    )
    # Opens the actual TCP connection to Postgres.
    #   - ssl_context=True tells pg8000 to negotiate an SSL/TLS
    #     connection — required right now since our Cloud SQL instance
    #     is configured with ssl_mode = "ENCRYPTED_ONLY" (Step 4.8,
    #     Terraform change). ssl_context=None disables SSL, which
    #     would only be appropriate over a private-IP-only connection
    #     inside the VPC where GCP's network boundary itself provides
    #     the security guarantee (still worth using SSL there too in
    #     a real production system — noted here as a simplification
    #     for this dev setup, not a recommendation to skip it later).

    try:
        yield conn
        # Hands the open connection to whatever code is inside the
        # `with get_connection() as conn:` block. Execution pauses
        # here until that block finishes.
    finally:
        conn.close()
        # Guaranteed to run once the `with` block exits — whether it
        # completed normally or raised an exception. This is exactly
        # the guarantee @contextmanager + try/finally gives us, and
        # it's what prevents leaked, dangling open connections if
        # something inside the `with` block fails.


# --- WRITE FUNCTIONS ---

def insert_document(gcs_uri: str, original_filename: str, page_count: int) -> str:
    """
    Inserts one row into the `documents` table and returns its
    generated UUID (as a string) — the foreign key that every chunk
    belonging to this document will reference.
    """

    with get_connection() as conn:
        cursor = conn.cursor()
        # A cursor is what actually executes SQL statements and lets
        # you fetch results, over an open connection. Standard DB-API
        # pattern — one cursor per unit of work here.

        cursor.execute(
            """
            INSERT INTO documents (gcs_uri, original_filename, page_count)
            VALUES (%s, %s, %s)
            RETURNING id;
            """,
            (gcs_uri, original_filename, page_count),
        )
        # Parameterized query: the %s placeholders are filled in by
        # the driver from the tuple (gcs_uri, original_filename,
        # page_count), NOT via Python string formatting/f-strings.
        # This is a critical security practice — it prevents SQL
        # injection, since the driver sends the values separately
        # from the SQL text rather than concatenating user-controlled
        # strings directly into the query. Never build SQL with
        # f-strings when values come from outside the code.
        #
        # RETURNING id asks Postgres to hand back the auto-generated
        # UUID from this INSERT immediately, so we don't need a
        # separate SELECT to find out what ID was just created.

        document_id = cursor.fetchone()[0]
        # fetchone() returns the next row of results as a tuple —
        # here, just one row with one column (the id), so [0] pulls
        # that single value out.

        conn.commit()
        # Explicitly commits the transaction, making this INSERT
        # permanent. pg8000 (like most DB-API drivers) does not
        # auto-commit by default — without this line, the insert
        # would be rolled back when the connection closes.

        logger.info("document_inserted", document_id=str(document_id), gcs_uri=gcs_uri)

        return str(document_id)
        # UUIDs come back from pg8000 as Python UUID objects; we
        # convert to str for a simpler, JSON-serializable return type
        # that's easy to pass around and log.


def insert_chunks(document_id: str, embedded_chunks: list[EmbeddedChunk]) -> int:
    """
    Inserts all chunks for a given document in a single transaction.
    Returns the number of chunks inserted.

    All-or-nothing behavior: if any chunk fails to insert, the whole
    batch is rolled back — we never want a document left with only
    some of its chunks persisted, since that would silently produce
    incomplete (and misleading) retrieval results later.
    """

    with get_connection() as conn:
        cursor = conn.cursor()

        for item in embedded_chunks:
            embedding_str = "[" + ",".join(str(x) for x in item.embedding) + "]"
            # pg8000 doesn't have native support for pgvector's custom
            # `vector` type — as far as the driver is concerned, it's
            # just sending a value. pgvector accepts a specific text
            # format for vectors: "[0.123,0.456,...]" — a
            # comma-separated list inside square brackets, as a
            # string. This line builds exactly that string from our
            # Python list[float]. The SQL query below then explicitly
            # casts this string to the `vector` type with `::vector`.

            cursor.execute(
                """
                INSERT INTO chunks
                    (document_id, chunk_index, char_start, char_end, text, embedding)
                VALUES
                    (%s, %s, %s, %s, %s, %s::vector);
                """,
                (
                    document_id,
                    item.chunk.chunk_index,
                    item.chunk.char_start,
                    item.chunk.char_end,
                    item.chunk.text,
                    embedding_str,
                ),
            )
            # Same parameterized-query pattern as insert_document.
            # %s::vector — the %s placeholder gets filled with our
            # embedding_str, and ::vector casts that string into
            # pgvector's native vector type at insert time. This is
            # the one place in this whole file where pgvector-specific
            # syntax shows up; everywhere else this is completely
            # ordinary SQL.

        conn.commit()
        # Commits all inserts from the loop above as ONE transaction —
        # this is what gives us the all-or-nothing guarantee described
        # in the docstring. If any single cursor.execute() call above
        # raised an exception, we'd never reach this commit() line,
        # and none of the inserts in this batch would be persisted
        # (pg8000 rolls back automatically when the connection closes
        # without a commit, or you could add an explicit except block
        # with conn.rollback() for clearer intent — worth adding once
        # this moves toward production-hardening in a later step).

        logger.info(
            "chunks_inserted",
            document_id=document_id,
            chunk_count=len(embedded_chunks),
        )

        return len(embedded_chunks)


def persist_document(
    gcs_uri: str,
    original_filename: str,
    page_count: int,
    embedded_chunks: list[EmbeddedChunk],
) -> str:
    """
    Convenience wrapper: inserts the document row, then all its
    chunks, in sequence. This is the single function the ingestion
    pipeline script will call — callers don't need to know about
    insert_document/insert_chunks individually.
    """

    document_id = insert_document(gcs_uri, original_filename, page_count)
    insert_chunks(document_id, embedded_chunks)

    logger.info(
        "document_persisted",
        document_id=document_id,
        gcs_uri=gcs_uri,
        chunk_count=len(embedded_chunks),
    )

    return document_id
