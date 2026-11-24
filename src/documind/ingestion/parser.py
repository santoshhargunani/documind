from typing import BinaryIO

import structlog
from google.api_core import exceptions as gexc
from google.api_core.client_options import ClientOptions
from google.cloud import documentai
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from documind.config import get_settings

logger = structlog.get_logger()

_PAGE_BATCH_SIZE = 10


def count_pdf_pages(pdf_file: BinaryIO) -> int:
    try:
        page_count = len(PdfReader(pdf_file).pages)
    except PdfReadError as exc:
        raise gexc.InvalidArgument(f"Unable to read PDF pages: {exc}") from exc

    if page_count < 1:
        raise gexc.InvalidArgument("PDF contains no pages")
    return page_count


@retry(
    retry=retry_if_not_exception_type(gexc.InvalidArgument),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def parse_document(
    gcs_uri: str, page_count: int, mime_type: str = "application/pdf"
) -> tuple[str, int]:
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

    if page_count < 1:
        raise gexc.InvalidArgument("PDF contains no pages")

    text_parts: list[str] = []
    processed_page_count = 0
    logger.info(
        "parsing_document",
        gcs_uri=gcs_uri,
        processor=processor_name,
        page_count=page_count,
        page_batch_size=_PAGE_BATCH_SIZE,
    )

    for first_page in range(1, page_count + 1, _PAGE_BATCH_SIZE):
        page_numbers = list(
            range(first_page, min(first_page + _PAGE_BATCH_SIZE, page_count + 1))
        )
        request = documentai.ProcessRequest(
            name=processor_name,
            gcs_document=gcs_document,
            process_options=documentai.ProcessOptions(
                individual_page_selector=documentai.ProcessOptions.IndividualPageSelector(
                    pages=page_numbers
                )
            ),
            imageless_mode=True,
        )

        result = client.process_document(request=request)
        batch_page_count = len(result.document.pages)
        if batch_page_count != len(page_numbers):
            raise gexc.InvalidArgument(
                f"Document AI returned {batch_page_count} pages for requested "
                f"pages {first_page}-{page_numbers[-1]}"
            )

        if result.document.text:
            text_parts.append(result.document.text)
        processed_page_count += batch_page_count

        logger.info(
            "document_page_batch_parsed",
            gcs_uri=gcs_uri,
            page_start=first_page,
            page_end=page_numbers[-1],
            page_count=batch_page_count,
        )

    extracted_text = "\n".join(text_parts)

    logger.info(
        "parse_complete",
        gcs_uri=gcs_uri,
        text_length=len(extracted_text),
        page_count=processed_page_count,
    )

    return extracted_text, processed_page_count