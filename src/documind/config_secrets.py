"""
config_secrets.py  --  Fetches secrets from Google Secret Manager at runtime.

PLACE AT: src/documind/config_secrets.py   (REPLACES the existing file)

WHAT CHANGED
  + get_app_db_password(): returns the password of the low-privilege app role.
  get_db_password() (admin) is unchanged and kept for admin tooling.
"""

# lru_cache: each secret is fetched from Secret Manager once per process, then
# served from memory (cheaper and faster than a network call per DB connection).
from functools import lru_cache

# Google's Secret Manager client library.
from google.cloud import secretmanager

# tenacity gives us declarative retries. Secret Manager calls can fail
# transiently (network blip, brief 5xx), so we retry a few times.
from tenacity import retry, stop_after_attempt, wait_exponential

# Our typed settings (project id and secret names come from here).
from documind.config import get_settings


@retry(
    stop=stop_after_attempt(3),                              # give up after 3 attempts
    wait=wait_exponential(multiplier=1, min=1, max=10),      # wait 1s, 2s, 4s... capped at 10s
)
def _fetch_secret(secret_id: str) -> str:
    """Read the LATEST version of a secret and return its value as text."""
    settings = get_settings()
    # Client uses Application Default Credentials: your gcloud login locally,
    # Workload Identity in GKE/Cloud Run (no key files).
    client = secretmanager.SecretManagerServiceClient()
    # Fully-qualified resource name of the secret version we want.
    name = f"projects/{settings.gcp_project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    # The payload arrives as bytes; decode to a normal string.
    return response.payload.data.decode("UTF-8")


@lru_cache
def get_db_password() -> str:
    """Password of the ADMIN database user (for migrations/admin tooling only)."""
    settings = get_settings()
    return _fetch_secret(settings.db_password_secret_id)


@lru_cache
def get_app_db_password() -> str:
    """Password of the low-privilege APP role (documind_app) that RLS applies to."""
    settings = get_settings()
    return _fetch_secret(settings.db_app_password_secret_id)
