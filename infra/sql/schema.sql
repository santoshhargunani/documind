-- =============================================================================
-- schema.sql  --  DocuMind multi-tenant, event-driven schema
-- PLACE AT: infra/sql/schema.sql   (REPLACES the old single-tenant version)
--
-- WHAT CHANGED vs. the old schema
--   1. New `tenants` table: one row per customer.
--   2. `documents` and `chunks` carry a `tenant_id` on every row.
--   3. `documents` gets lifecycle columns (generation, status, lease, ...) so a
--      Pub/Sub-triggered worker can safely handle duplicate / retried messages.
--   4. Row-Level Security (RLS): Postgres itself refuses to show or write rows
--      that belong to a different tenant than the one set for the transaction.
--   5. A low-privilege role `documind_app` that the application connects as.
--      (RLS does not constrain superusers or roles with BYPASSRLS, so the app
--      must NOT connect as your admin user.)
--
-- Because your environment is destroyed and recreated each session, this file
-- is the single source of truth: apply it to a fresh database every session.
-- It is written for a FRESH database. If the OLD tables still exist, run once:
--   DROP TABLE IF EXISTS chunks, documents CASCADE;
-- (dev data only -- it is repopulated by ingestion).
-- =============================================================================


-- pgvector: adds the `vector` column type and similarity operators/indexes.
-- IF NOT EXISTS makes re-running this file harmless.
CREATE EXTENSION IF NOT EXISTS vector;


-- -----------------------------------------------------------------------------
-- TENANTS: one row per customer organisation.
-- -----------------------------------------------------------------------------
CREATE TABLE tenants (
    -- Stable machine id, also used as the GCS folder name (gs://bucket/<tenant_id>/...).
    -- The CHECK mirrors the regex in auth.py so a malformed id can never be stored:
    -- lowercase letters/digits/hyphens, 2-63 chars, starts with letter or digit.
    tenant_id           TEXT PRIMARY KEY
                        CHECK (tenant_id ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    -- Human-readable customer name for dashboards/logs.
    name                TEXT NOT NULL,
    -- 'pooled' = shared tables + RLS (today's model). 'silo' reserves room for a
    -- customer who later demands a dedicated database/project.
    isolation_tier      TEXT NOT NULL DEFAULT 'pooled'
                        CHECK (isolation_tier IN ('pooled', 'silo')),
    -- 'suspended' lets us block a tenant's ingestion/queries without deleting data.
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'suspended')),
    -- Noisy-neighbour control: max documents this tenant may have in flight at once.
    -- Enforced by the worker at claim time (added in a later step).
    max_concurrent_docs INT NOT NULL DEFAULT 2 CHECK (max_concurrent_docs > 0),
    -- Optional monthly page budget (NULL = unlimited) to cap Document AI/embedding cost.
    monthly_page_quota  INT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- -----------------------------------------------------------------------------
-- DOCUMENTS: one row per uploaded PDF (per tenant + object path).
-- -----------------------------------------------------------------------------
CREATE TABLE documents (
    -- Internal surrogate id; chunks point at this.
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Owning tenant. ON DELETE CASCADE = deleting a tenant removes all its documents.
    tenant_id         TEXT NOT NULL REFERENCES tenants (tenant_id) ON DELETE CASCADE,
    -- Full object location, e.g. gs://documind-uploads/acme/policy.pdf
    gcs_uri           TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    page_count        INT,
    -- GCS "generation" number: GCS assigns a new one every time an object is
    -- (over)written, so it doubles as a version id. The worker uses it to tell
    -- a duplicate message (same generation) from an update (higher generation)
    -- and to ignore stale messages (lower generation).
    generation        BIGINT NOT NULL,
    -- SHA-256 of the file bytes. Lets us skip re-embedding if an "update" is
    -- byte-identical to what we already indexed.
    content_hash      TEXT,
    -- Lifecycle:  processing -> ready   (success)
    --             processing -> failed  (permanent error, e.g. corrupt PDF)
    status            TEXT NOT NULL DEFAULT 'processing'
                      CHECK (status IN ('processing', 'ready', 'failed')),
    -- How many times a worker has claimed this generation (retry accounting).
    attempts          INT NOT NULL DEFAULT 0,
    -- Most recent error message, for debugging and the DLQ replay tooling.
    last_error        TEXT,
    -- A worker "leases" a document while processing it. If the worker dies, the
    -- lease expires and a redelivered message may claim it again. Prevents two
    -- workers processing the same document at the same time.
    lease_expires_at  TIMESTAMPTZ,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One row per object path per tenant. Overwrites UPDATE this row (bumping
    -- generation) instead of creating duplicates. Also the target of the
    -- worker's INSERT ... ON CONFLICT claim.
    UNIQUE (tenant_id, gcs_uri),
    -- Needed so `chunks` can reference (id, tenant_id) as a pair (see below).
    UNIQUE (id, tenant_id)
);

-- Fast "what is in flight / failed for this tenant" lookups (concurrency caps, dashboards).
CREATE INDEX documents_tenant_status_idx ON documents (tenant_id, status);


-- -----------------------------------------------------------------------------
-- CHUNKS: text pieces of a document plus their embedding vectors.
-- -----------------------------------------------------------------------------
CREATE TABLE chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- tenant_id is DUPLICATED here on purpose (denormalised): RLS and vector
    -- search can filter chunks by tenant directly without joining documents.
    tenant_id   TEXT NOT NULL,
    document_id UUID NOT NULL,
    chunk_index INT  NOT NULL,
    char_start  INT  NOT NULL,
    char_end    INT  NOT NULL,
    text        TEXT NOT NULL,
    -- 768-dimension embedding, same size as before.
    embedding   vector(768) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Composite foreign key: a chunk's (document_id, tenant_id) must match a real
    -- document's (id, tenant_id). This makes it IMPOSSIBLE for a chunk to be
    -- stored under a different tenant than its parent document, even by a bug.
    -- ON DELETE CASCADE = deleting a document (or tenant) removes its chunks.
    FOREIGN KEY (document_id, tenant_id)
        REFERENCES documents (id, tenant_id) ON DELETE CASCADE
);

-- Approximate nearest-neighbour index for cosine similarity search (unchanged).
CREATE INDEX chunks_embedding_idx ON chunks
    USING hnsw (embedding vector_cosine_ops);

-- Fast per-tenant, per-document lookups and deletes (used when replacing chunks on update).
CREATE INDEX chunks_tenant_document_idx ON chunks (tenant_id, document_id);


-- -----------------------------------------------------------------------------
-- ROW-LEVEL SECURITY
-- The application sets the tenant for each transaction with:
--     SET LOCAL app.tenant_id = '<tenant>';
-- and the policies below compare every row's tenant_id to that setting.
--   * current_setting(name, true): the `true` means "return NULL instead of
--     erroring if the setting was never set". NULL = no match = zero rows, so a
--     forgotten SET LOCAL FAILS CLOSED (shows nothing) instead of leaking.
--   * USING       : which existing rows a statement may see/update/delete.
--   * WITH CHECK  : which new/changed rows a statement may write.
-- -----------------------------------------------------------------------------

-- documents: ENABLE turns RLS on; FORCE makes it apply even to the table owner,
-- so accidentally connecting as the owner can't silently bypass the wall.
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON documents
    USING      (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- chunks: same policy, same reasoning.
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON chunks
    USING      (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- tenants: a tenant context may read ONLY its own row (so the worker can verify
-- "does this tenant exist and is it active?" without being able to list others).
-- Deliberately NOT forced, so your admin/owner can still create and manage tenants.
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_self_read ON tenants
    FOR SELECT
    USING (tenant_id = current_setting('app.tenant_id', true));


-- -----------------------------------------------------------------------------
-- APPLICATION ROLE
-- A plain role with no special attributes: not a superuser, no BYPASSRLS, not the
-- table owner -> RLS is fully enforced for it. (New roles get these safe defaults;
-- we intentionally do not ALTER those attributes, Cloud SQL does not permit it
-- for non-superusers.) The password is set by a separate step, not in this file,
-- so no secret ever lives in source control.
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    -- Create the role only if it doesn't already exist (e.g. made by Terraform).
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'documind_app') THEN
        CREATE ROLE documind_app LOGIN;
    END IF;
END
$$;

-- Allow the role to use the default schema.
GRANT USAGE ON SCHEMA public TO documind_app;
-- Read-only on tenants (RLS limits it to the caller's own row anyway).
GRANT SELECT ON tenants TO documind_app;
-- Normal read/write on the data tables, still filtered by the RLS policies above.
GRANT SELECT, INSERT, UPDATE, DELETE ON documents, chunks TO documind_app;

-- Convenience for testing (verify_rls.sql): lets the admin user temporarily act
-- as documind_app with SET ROLE. Wrapped so a permission error is only a notice.
DO $$
BEGIN
    EXECUTE format('GRANT documind_app TO %I', current_user);
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'could not grant documind_app to %; SET ROLE testing may not work', current_user;
END
$$;


-- -----------------------------------------------------------------------------
-- SEED TENANTS (demo/test). The owner bypasses the non-forced tenants RLS, so
-- this works. ON CONFLICT DO NOTHING keeps the file safe to re-run.
-- -----------------------------------------------------------------------------
INSERT INTO tenants (tenant_id, name) VALUES
    ('acme',   'Acme Corp'),
    ('globex', 'Globex Inc')
ON CONFLICT (tenant_id) DO NOTHING;
