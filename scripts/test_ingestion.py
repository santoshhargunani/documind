"""
test_ingestion.py  --  Manual end-to-end test of tenant-aware ingestion.

PLACE AT: scripts/test_ingestion.py   (REPLACES the old single-document test)

Run:   python scripts\\test_ingestion.py            # tenant "acme"
       python scripts\\test_ingestion.py globex     # a different tenant
Needs: a real PDF at scripts\\sample.pdf, terraform applied, schema.sql applied,
       the app-role password set (set_app_password.sql), and .env refreshed.

What it proves
  1. Upload + ingest works for a tenant.
  2. Running the SAME ingestion again is a harmless "duplicate" (idempotency).
  3. The tenant sees its chunks, and the OTHER tenant sees zero of them
     (Row-Level Security doing its job).
"""

# sys: read the optional tenant name from the command line.
import sys

# Path: cross-platform file paths (upload_document expects a Path).
from pathlib import Path

# Typed settings (we need the bucket name).
from documind.config import get_settings

# count_chunks: tenant-scoped chunk counter that relies purely on RLS.
from documind.db import count_chunks

# The reusable unit of work: ingest one object for the tenant in its path.
from documind.ingestion.pipeline import ingest_object

# Existing helper: uploads a local file to the raw-docs bucket, returns gs:// URI.
from documind.ingestion.storage import upload_document

# Tenant to test with: first CLI argument, default "acme".
tenant = sys.argv[1] if len(sys.argv) > 1 else "acme"
# A second tenant to prove isolation against.
other_tenant = "globex" if tenant != "globex" else "acme"

settings = get_settings()
# The tenant is the FIRST path segment: this is what pipeline.py trusts.
object_name = f"{tenant}/sample.pdf"

# --- 1. Upload into the tenant's folder ---
gcs_uri = upload_document(Path("scripts/sample.pdf"), object_name)
print(f"Uploaded to: {gcs_uri}")

# --- 2. Ingest (parse -> chunk -> embed -> persist) ---
first = ingest_object(settings.raw_docs_bucket, object_name)
print(f"\nFirst run : {first.status}  chunks={first.chunk_count}  {first.detail}")

# --- 3. Ingest again: must be recognised as already done ---
second = ingest_object(settings.raw_docs_bucket, object_name)
print(f"Second run: {second.status}  (expected: duplicate)")

# --- 4. Isolation check ---
print(f"\nChunks visible to {tenant:>8}: {count_chunks(tenant)}")
print(f"Chunks visible to {other_tenant:>8}: {count_chunks(other_tenant)}  (expected: 0)")
