from documind.config import get_settings
from documind.config_secrets import get_db_password

settings = get_settings()
print(f"Project: {settings.gcp_project_id}")
print(f"Bucket: {settings.raw_docs_bucket}")
print(f"Chunk size: {settings.chunk_size}")

password = get_db_password()
print(f"DB password fetched: {len(password)} characters")