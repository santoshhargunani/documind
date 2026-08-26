import structlog
from google.api_core.client_options import ClientOptions
from google.cloud import documentai
from tenacity import retry, stop_after_attempt, wait_exponential

from documind.config import get_settings

logger = structlog.get_logger()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
)
def parse_document(gcs_uri: str, mime_type: str = "application/pdf") -> str:
    settings = get_settings()

    client_options = ClientOptions(
        api_endpoint=f"{settings.docai_location}-documentai.googleapis.com"
    )
    client = documentai.DocumentProcessorServiceClient(client_options=client_options)

    processor_name = client.processor_path(
        settings.gcp_project_id,
        settings.docai_location,
        settings.docai_processor_id,
    )

    gcs_document = documentai.GcsDocument(gcs_uri=gcs_uri, mime_type=mime_type)

    request = documentai.ProcessRequest(
        name=processor_name,
        gcs_document=gcs_document,
    )

    logger.info("parsing_document", gcs_uri=gcs_uri, processor=processor_name)

    result = client.process_document(request=request)
    extracted_text = result.document.text

    logger.info(
        "parse_complete",
        gcs_uri=gcs_uri,
        text_length=len(extracted_text),
        page_count=len(result.document.pages),
    )

    return extracted_text, len(result.document.pages)