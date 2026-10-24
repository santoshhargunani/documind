-- =============================================================================
-- verify_rls.sql  --  Proves tenant isolation works at the database level.
-- PLACE AT: infra/sql/verify_rls.sql
-- RUN WITH: psql "<same connection string as schema.sql>" -f infra\sql\verify_rls.sql
--
-- Everything runs inside ONE transaction that is ROLLED BACK at the end, so it
-- leaves no data behind. The last INSERT is EXPECTED to fail (that is the proof).
-- =============================================================================

BEGIN;                                   -- start a transaction we will undo

-- Act as the low-privilege application role. (Your admin user is exempt from RLS,
-- so testing as admin would prove nothing.) SET LOCAL ROLE lasts until ROLLBACK.
SET LOCAL ROLE documind_app;

-- Pretend we are a request from tenant "acme". SET LOCAL = this transaction only.
SET LOCAL app.tenant_id = 'acme';

\echo '--- 1. acme inserts a document (should succeed) ---'
INSERT INTO documents (tenant_id, gcs_uri, original_filename, generation)
VALUES ('acme', 'gs://test-bucket/acme/a.pdf', 'a.pdf', 1);

\echo '--- 2. acme sees its own document (expect count = 1) ---'
SELECT count(*) AS acme_sees FROM documents;

-- Switch the "caller" to a different tenant, in the same transaction.
SET LOCAL app.tenant_id = 'globex';

\echo '--- 3. globex looks at documents (expect count = 0: acme row is invisible) ---'
SELECT count(*) AS globex_sees FROM documents;

\echo '--- 4. globex tries to write a row labelled acme (EXPECT AN ERROR below) ---'
-- Should fail with: new row violates row-level security policy for table "documents"
INSERT INTO documents (tenant_id, gcs_uri, original_filename, generation)
VALUES ('acme', 'gs://test-bucket/acme/evil.pdf', 'evil.pdf', 1);

-- Undo everything (also clears the aborted-transaction state from step 4).
ROLLBACK;

\echo '--- 5. with NO tenant set, the app role sees nothing (fails closed) ---'
BEGIN;
SET LOCAL ROLE documind_app;
SELECT count(*) AS unset_sees FROM documents;   -- expect 0
ROLLBACK;

\echo '--- 6. role attributes: expect rolsuper = f and rolbypassrls = f ---'
SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'documind_app';
