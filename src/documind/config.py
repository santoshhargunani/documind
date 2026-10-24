"""
config.py  --  Central, typed application settings for DocuMind.

PLACE AT: src/documind/config.py   (REPLACES the existing file)

WHAT CHANGED
  + db_app_user / db_app_password_secret_id : the low-privilege role the APP
    connects as (RLS only protects tenants if the app is NOT the admin/owner).
  + ingest_lease_seconds / ingest_max_attempts / ingest_max_pdf_mb : knobs for
    the event-driven ingestion worker.
  The new fields have defaults, so your existing .env keeps working unchanged.
"""

# lru_cache memoises a function's result so Settings is built once per process,
# not re-read from the environment on every call.
from functools import lru_cache

# Field lets us bind each attribute to a specific environment-variable name
# (alias) and give it a default. Pydantic also validates types for us.
from pydantic import Field

# BaseSettings = a Pydantic model that loads its values from environment
# variables / a .env file. SettingsConfigDict configures how it loads them.
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration for DocuMind, loaded from environment variables / .env."""

    # How to load: read ".env" in the working directory, as UTF-8, and silently
    # ignore any variables in it that this class does not declare.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Google Cloud basics ---
    gcp_project_id: str = Field(alias="GCP_PROJECT_ID")                    # project to call APIs in
    gcp_region: str = Field(default="us-central1", alias="GCP_REGION")      # default region

    # --- Storage and Document AI ---
    raw_docs_bucket: str = Field(alias="RAW_DOCS_BUCKET")                   # bucket tenants upload PDFs to
    docai_processor_id: str = Field(alias="DOCAI_PROCESSOR_ID")             # changes after every terraform apply
    docai_location: str = Field(default="us", alias="DOCAI_LOCATION")       # Document AI region

    # --- Database connection ---
    db_instance_connection_name: str = Field(alias="DB_INSTANCE_CONNECTION_NAME")
    db_name: str = Field(default="documind", alias="DB_NAME")
    # ADMIN user: used only by humans/migrations (psql, schema.sql). NOT used by app code anymore.
    db_user: str = Field(default="documind_admin", alias="DB_USER")
    db_password_secret_id: str = Field(alias="DB_PASSWORD_SECRET_ID")       # secret holding the admin password
    db_host: str = Field(alias="DB_HOST")                                   # public IP locally, private IP in GKE/Cloud Run
    db_port: int = Field(default=5432, alias="DB_PORT")
    db_sslmode: str = Field(default="require", alias="DB_SSLMODE")

    # APP user: the role application code connects as. It is a normal role with
    # no BYPASSRLS, so Row-Level Security applies to it. Created by schema.sql;
    # its password is created by Terraform and stored in this secret.
    db_app_user: str = Field(default="documind_app", alias="DB_APP_USER")
    db_app_password_secret_id: str = Field(
        default="documind-postgres-app-password", alias="DB_APP_PASSWORD_SECRET_ID"
    )

    # --- Embedding / chunking ---
    embedding_model: str = Field(default="text-embedding-005", alias="EMBEDDING_MODEL")
    chunk_size: int = Field(default=1000, alias="CHUNK_SIZE")
    chunk_overlap: int = Field(default=150, alias="CHUNK_OVERLAP")

    # --- Event-driven ingestion worker ---
    # How long a worker "owns" a document while processing it. Must be LONGER than
    # the Pub/Sub ack deadline (max 600s) so a slow-but-alive worker isn't pre-empted.
    ingest_lease_seconds: int = Field(default=900, alias="INGEST_LEASE_SECONDS")
    # After this many claims of the same document version we stop retrying and
    # mark it failed (prevents infinite retry loops on a poison document).
    ingest_max_attempts: int = Field(default=5, alias="INGEST_MAX_ATTEMPTS")
    # Reject PDFs bigger than this (cost/abuse protection).
    ingest_max_pdf_mb: int = Field(default=50, alias="INGEST_MAX_PDF_MB")


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings object (built once, then cached)."""
    return Settings()
