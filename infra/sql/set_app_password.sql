-- =============================================================================
-- set_app_password.sql  --  Sets the password of the low-privilege app role.
-- PLACE AT: infra/sql/set_app_password.sql
--
-- WHY A SEPARATE FILE: schema.sql is committed to git and must never contain a
-- secret. Terraform generates the app password and stores it in Secret Manager
-- (documind-postgres-app-password); this file applies that value to the role.
--
-- RUN WITH (PowerShell), passing the password as a psql variable:
--   $env:PGPASSWORD = gcloud secrets versions access latest --secret=documind-postgres-password
--   $appPw = gcloud secrets versions access latest --secret=documind-postgres-app-password
--   psql "host=<public_ip> port=5432 dbname=documind user=<admin user> sslmode=require" -v "app_pw=$appPw" -f infra\sql\set_app_password.sql
--
-- :'app_pw' is psql's syntax for "insert the variable app_pw as a safely quoted
-- string literal", so special characters in the password cannot break the SQL.
-- =============================================================================

ALTER ROLE documind_app WITH PASSWORD :'app_pw';
