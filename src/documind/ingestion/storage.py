from pathlib import Path

import structlog
from google.cloud import storage
from tenacity import retry, stop_after_attempt, wait_exponential

from documind.config import get_settings

logger = structlog.get_logger()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
)
def upload_document(local_path: Path, destination_blob_name: str) -> str:
    settings = get_settings()
    client = storage.Client(project=settings.gcp_project_id)
    bucket = client.bucket(settings.raw_docs_bucket)
    blob = bucket.blob(destination_blob_name)

    logger.info(
        "uploading_document",
        local_path=str(local_path),
        destination=destination_blob_name,
        bucket=settings.raw_docs_bucket,
    )

    blob.upload_from_filename(str(local_path))

    gcs_uri = f"gs://{settings.raw_docs_bucket}/{destination_blob_name}"
    logger.info("upload_complete", gcs_uri=gcs_uri)
    return gcs_uri