"""Add diagnostic labels without changing test outcomes or expectations."""

from typing import Any


def failure_label(result: dict[str, Any]) -> str | None:
    if result.get("status") not in {"FAIL", "ERROR"}:
        return None
    reason = result.get("reason", "")
    if result.get("trace", {}).get("remote_job_may_continue") or reason in {
        "transport_or_deadline_error",
        "transport_error",
        "absolute_deadline_exceeded",
    }:
        return "transport_or_remote_completion"
    labels = {
        "answer_mismatch": "answer_mismatch",
        "incomplete_completion": "incomplete_model_answer",
        "invalid_answer_json": "invalid_model_answer",
        "invalid_answer_shape": "invalid_model_answer",
        "invalid_answer": "invalid_model_answer",
        "invalid_completion": "invalid_model_answer",
        "expected_rejection_did_not_match": "rejection_contract_mismatch",
        "content_mismatch": "extraction_reference_mismatch",
        "invalid_document_or_reference": "document_validation",
        "partial_or_invalid_conversion_result": "partial_or_invalid_extraction",
        "conversion_not_success": "native_conversion_failed",
    }
    if reason in labels:
        return labels[reason]
    if reason == "unexpected_http_status":
        return "processing_limit" if result.get("http_status") == 413 else "http_failure"
    return "needs_diagnosis"
