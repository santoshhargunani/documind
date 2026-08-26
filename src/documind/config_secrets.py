from functools import lru_cache

from google.cloud import secretmanager
from tenacity import retry, stop_after_attempt, wait_exponential

from documind.config import get_settings


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
)
def _fetch_secret(secret_id: str) -> str:
    settings = get_settings()
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{settings.gcp_project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")


@lru_cache
def get_db_password() -> str:
    settings = get_settings()
    return _fetch_secret(settings.db_password_secret_id)