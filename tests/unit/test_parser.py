from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from google.api_core.exceptions import InvalidArgument

from documind.ingestion import parser


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
        parser.parse_document("gs://bucket/document.pdf")

    client.process_document.assert_called_once()
    request = client.process_document.call_args.kwargs["request"]
    assert request.imageless_mode is True
