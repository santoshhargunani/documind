from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from google.api_core.exceptions import InvalidArgument
from pypdf import PdfWriter

from documind.ingestion import parser


def test_count_pdf_pages_returns_total_page_count() -> None:
    writer = PdfWriter()
    for _ in range(34):
        writer.add_blank_page(width=72, height=72)

    pdf_file = BytesIO()
    writer.write(pdf_file)
    pdf_file.seek(0)

    assert parser.count_pdf_pages(pdf_file) == 34


def test_parse_document_processes_pages_in_batches_and_combines_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Mock()
    client.processor_path.return_value = "processor"
    client.process_document.side_effect = [
        SimpleNamespace(document=SimpleNamespace(text="pages 1-10", pages=[None] * 10)),
        SimpleNamespace(document=SimpleNamespace(text="pages 11-20", pages=[None] * 10)),
        SimpleNamespace(document=SimpleNamespace(text="pages 21-30", pages=[None] * 10)),
        SimpleNamespace(document=SimpleNamespace(text="pages 31-34", pages=[None] * 4)),
    ]
    monkeypatch.setattr(
        parser,
        "get_settings",
        lambda: SimpleNamespace(
            docai_location="us",
            gcp_project_id="test-project",
            docai_processor_id="test-processor",
        ),
    )
    monkeypatch.setattr(
        parser.documentai,
        "DocumentProcessorServiceClient",
        lambda **_: client,
    )

    text, page_count = parser.parse_document("gs://bucket/document.pdf", page_count=34)

    assert text == "pages 1-10\npages 11-20\npages 21-30\npages 31-34"
    assert page_count == 34
    requests = [call.kwargs["request"] for call in client.process_document.call_args_list]
    assert [
        list(request.process_options.individual_page_selector.pages) for request in requests
    ] == [
        list(range(1, 11)),
        list(range(11, 21)),
        list(range(21, 31)),
        list(range(31, 35)),
    ]
    assert all(request.imageless_mode for request in requests)


def test_parse_document_surfaces_invalid_argument_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Mock()
    client.processor_path.return_value = "processor"
    client.process_document.side_effect = InvalidArgument("invalid PDF request")
    monkeypatch.setattr(
        parser,
        "get_settings",
        lambda: SimpleNamespace(
            docai_location="us",
            gcp_project_id="test-project",
            docai_processor_id="test-processor",
        ),
    )
    monkeypatch.setattr(
        parser.documentai,
        "DocumentProcessorServiceClient",
        lambda **_: client,
    )

    with pytest.raises(InvalidArgument, match="invalid PDF request"):
        parser.parse_document("gs://bucket/document.pdf", page_count=1)

    client.process_document.assert_called_once()
    request = client.process_document.call_args.kwargs["request"]
    assert request.imageless_mode is True
    assert list(request.process_options.individual_page_selector.pages) == [1]
