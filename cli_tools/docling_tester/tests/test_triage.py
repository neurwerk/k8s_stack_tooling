import pytest

from docling_tester.triage import failure_label


@pytest.mark.parametrize(
    "reason,status_code,label",
    [
        ("answer_mismatch", 200, "answer_mismatch"),
        ("incomplete_completion", 200, "incomplete_model_answer"),
        ("expected_rejection_did_not_match", 400, "rejection_contract_mismatch"),
        ("unexpected_http_status", 413, "processing_limit"),
        ("unexpected_http_status", 503, "http_failure"),
        ("invalid_document_or_reference", 200, "document_validation"),
        ("conversion_not_success", 200, "native_conversion_failed"),
        ("unrecognized_failure", 200, "needs_diagnosis"),
    ],
)
def test_labels_do_not_reclassify_results(reason, status_code, label):
    result = {"status": "FAIL", "reason": reason, "http_status": status_code}
    original = dict(result)
    assert failure_label(result) == label
    assert result == original
    for status in ("PASS", "SKIPPED", "UNVERIFIED", "EXPECTED_REJECTION"):
        assert failure_label({**result, "status": status}) is None
