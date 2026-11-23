"""Serial document runs with durable results and explicit evidence requirements."""

from __future__ import annotations

import asyncio
import csv
import fnmatch
import hashlib
import io
import json
import math
import os
import statistics
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from docling_tester import __version__
from docling_tester.compare import Document, compare_documents, differences, normalized
from docling_tester.corpus import (
    GATEWAY_FILES,
    GATEWAY_IMAGES,
    Case,
    Corpus,
    contained,
    digest,
    reference_for,
)
from docling_tester.transport import (
    Direct,
    ProtocolError,
    Reply,
    Transport,
    captured_text,
    endpoint,
    gateway_payload,
    reason_code,
)
from docling_tester.triage import failure_label
from docling_tester.upstream_assertions import CONTRACTS


@dataclass
class Settings:
    mode: str
    url: str
    output: Path
    key: str = field(default="", repr=False)
    timeout: float = 360
    document_timeout: float = 300
    direct_page_limit: int = 250
    gateway_page_limit: int = 2
    skip_audio: bool = False
    reference_profile: str = "auto"
    model: str = ""
    auth_header: str = "Authorization"
    capture_url: str = ""
    capture_key: str = field(default="", repr=False)
    runtime_label: str = "unknown"
    formats: tuple[str, ...] = ()
    case_ids: tuple[str, ...] | None = None
    match: str = "*"
    expectations: Path | None = None
    max_response_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        self.url = endpoint(self.url)
        if self.capture_url:
            self.capture_url = endpoint(self.capture_url)
        if (
            self.mode not in {"direct", "gateway"}
            or self.timeout <= 0
            or not math.isfinite(self.timeout)
            or self.document_timeout <= 0
            or not math.isfinite(self.document_timeout)
            or self.direct_page_limit < 1
            or self.gateway_page_limit < 1
            or self.max_response_bytes < 1
        ):
            raise ValueError("Invalid run mode or timeout")
        if self.mode == "gateway" and not self.model:
            raise ValueError("Gateway mode requires --model")
        if self.capture_url and not self.capture_key:
            raise ValueError("Capture retrieval requires its reader credential")

    def options(self) -> dict[str, str]:
        value = {
            "target_type": "inbody",
            "to_formats": "json",
            "abort_on_error": "true",
            "image_export_mode": "placeholder",
            "include_images": "false",
            "include_page_images": "false",
            "pipeline": "standard",
            "layout_preset": "default",
            "ocr_preset": "default",
            "table_structure_preset": "default",
            "do_ocr": "true",
            "do_table_structure": "true",
            "document_timeout": str(self.document_timeout),
            "do_code_enrichment": "false",
            "do_formula_enrichment": "false",
            "do_chart_extraction": "false",
            "do_picture_classification": "false",
            "do_picture_description": "false",
            "do_pdf_heading_hierarchy": "false",
        }
        return value


class Artifacts:
    def __init__(self, root: Path, secrets: tuple[str, ...]):
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.root, self.secrets = root, tuple(secret for secret in secrets if secret)

    def scrub(self, text: str) -> str:
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    def _write(self, name: str, text: str) -> None:
        path = self.root / (name + ".tmp")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        path.replace(self.root / name)

    def write(self, name: str, text: str) -> None:
        self._write(name, self.scrub(text))

    def json(self, name: str, value: Any) -> None:
        def clean(item: Any) -> Any:
            if isinstance(item, str):
                return self.scrub(item)
            if isinstance(item, dict):
                return {self.scrub(str(key)): clean(val) for key, val in item.items()}
            if isinstance(item, (list, tuple)):
                return [clean(val) for val in item]
            return item

        self._write(name, json.dumps(clean(value), indent=2, ensure_ascii=False) + "\n")


def overrides(path: Path | None, cases: list[Case]) -> dict[str, Any]:
    if path is None:
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Expectations require schema_version 1")
    rules = value.get("cases")
    if not isinstance(rules, dict) or set(rules) - {case.path for case in cases}:
        raise ValueError("Expectations contain unknown cases")
    for modes in rules.values():
        if not isinstance(modes, dict) or set(modes) - {"direct", "gateway"}:
            raise ValueError("Invalid expectation mode")
        for mode, rule in modes.items():
            if not isinstance(rule, dict) or rule.get("outcome") not in {"convert", "reject"}:
                raise ValueError("Invalid expectation outcome")
            allowed = (
                {"outcome", "http_status", "reason"}
                if rule["outcome"] == "reject"
                else {"outcome", "reference", "sha256", "profile"}
            )
            if set(rule) - allowed:
                raise ValueError("Unknown expectation field")
            if rule["outcome"] == "reject":
                if type(rule.get("http_status")) is not int or not 400 <= rule["http_status"] < 500:
                    raise ValueError("Expected rejection needs an explicit 4xx status")
                if not isinstance(rule.get("reason"), str) or not rule["reason"]:
                    raise ValueError("Expected rejection needs a machine-readable reason")
            else:
                source = contained(path.parent, rule.get("reference", ""))
                if not source.is_file() or digest(source) != rule.get("sha256"):
                    raise ValueError("Expected output is missing or its checksum changed")
                if mode == "direct" and not rule.get("profile"):
                    raise ValueError("Direct reference override needs a profile")
    return rules


def approved_reference(settings: Settings, rule: dict[str, Any]) -> bytes:
    assert settings.expectations is not None
    data = contained(settings.expectations.parent, rule["reference"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != rule["sha256"]:
        raise ValueError("Approved reference changed")
    return data


def expectation(case: Case, settings: Settings, rules: dict[str, Any]) -> dict[str, Any]:
    configured = rules.get(case.path, {}).get(settings.mode)
    if configured is not None:
        return configured
    if settings.mode == "gateway" and case.format not in GATEWAY_FILES | GATEWAY_IMAGES:
        return {"outcome": "reject", "http_status": 400, "reason": "unsupported_format"}
    return {"outcome": "convert"}


def profile_for(case: Case, settings: Settings) -> str:
    if settings.reference_profile != "auto":
        return settings.reference_profile
    if case.format in {"pdf", "png", "jpg", "jpeg", "tif", "webp"}:
        return "standard-default-ocr"
    return "native"


def check_direct(
    corpus: Corpus,
    case: Case,
    settings: Settings,
    body: Any,
    rule: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    actual = body["document"]["json_content"]
    # Structural validity is required even when no usable reference exists.
    canonical = Document(actual).canonical()
    details: dict[str, Any] = {"pages": len(canonical["pages"])}
    profile = profile_for(case, settings)
    details["reference_profile"] = profile
    reference = reference_for(case, profile)
    if rule.get("reference"):
        assert settings.expectations is not None
        expected = json.loads(approved_reference(settings, rule))
        details["reference_sha256"] = rule["sha256"]
        if rule["profile"] != profile:
            return "UNVERIFIED", "reference_profile_mismatch", details
        details["reference"] = rule["reference"]
    elif reference is not None:
        expected = corpus.read_document(reference)
        details["reference"] = reference["path"]
        details["reference_sha256"] = corpus.files[reference["path"]]["sha256"]
    elif profile == "native" and (markdown_path := corpus.markdown_reference(case)) is not None:
        expected_markdown = corpus.read_markdown(markdown_path)
        actual_markdown = body["document"].get("md_content")
        details.update(
            reference=markdown_path,
            reference_sha256=corpus.files[markdown_path]["sha256"],
            verification_scope="upstream_markdown_export",
            markdown_export_options={"compact_tables": True},
        )
        if not isinstance(actual_markdown, str):
            return "FAIL", "markdown_output_missing", details
        # Upstream verify_export compares exact text after newline normalization.
        expected_markdown = expected_markdown.replace("\r\n", "\n").replace("\r", "\n")
        if case.format == "md":
            # Literal contract from test_backend_markdown.test_convert_valid:
            # only the stored expected export is stripped, never the response.
            expected_markdown = expected_markdown.rstrip()
        else:
            actual_markdown = actual_markdown.replace("\r\n", "\n").replace("\r", "\n")
        matches = expected_markdown == actual_markdown
        details["comparison"] = {
            "matches": matches,
            "differences": differences(expected_markdown, actual_markdown),
        }
        return (
            ("PASS", "markdown_matches_reference", details)
            if matches
            else ("FAIL", "markdown_mismatch", details)
        )
    elif profile == "native" and case.path in CONTRACTS:
        contract = CONTRACTS[case.path]
        source = contract["source"].split("::")[0]
        details.update(
            verification_scope="upstream_assertions",
            full_content_verified=False,
            assertion_source=contract["source"],
            assertion_source_sha256=corpus.files[source]["sha256"],
        )
        markdown = body["document"].get("md_content")
        if not isinstance(markdown, str):
            return "FAIL", "markdown_output_missing", details
        missing = [text for text in contract["required"] if text not in markdown]
        forbidden = [text for text in contract["forbidden"] if text in markdown]
        matches = not missing and not forbidden
        details["comparison"] = {
            "matches": matches,
            "missing_required": missing,
            "present_forbidden": forbidden,
        }
        return (
            ("PASS", "upstream_assertions_met", details)
            if matches
            else ("FAIL", "upstream_assertion_mismatch", details)
        )
    else:
        return "UNVERIFIED", "compatible_reference_missing", details
    try:
        Document(expected).canonical()
    except ValueError:
        return "UNVERIFIED", "reference_structure_not_supported", details
    comparison = compare_documents(expected, actual)
    details["comparison"] = comparison
    return (
        ("PASS", "content_matches_reference", details)
        if comparison["matches"]
        else ("FAIL", "content_mismatch", details)
    )


async def check_gateway(
    transport: Transport,
    settings: Settings,
    correlation: str,
    rule: dict[str, Any],
    artifacts: Artifacts,
    index: int,
) -> tuple[str, str, dict[str, Any]]:
    if not settings.capture_url:
        return "UNVERIFIED", "backend_capture_not_configured", {}
    capture = await transport.request(
        "GET",
        settings.capture_url + f"/captures/{correlation}",
        headers={"Authorization": f"Bearer {settings.capture_key}"},
    )
    if capture.status == 404:
        return "UNVERIFIED", "backend_capture_missing", {}
    if capture.status != 200:
        raise ProtocolError("capture_http_error")
    artifacts.json(f"{index:04d}.capture.json", capture.body)
    try:
        text = captured_text(capture.body, correlation)
    except ProtocolError as exc:
        if str(exc) == "capture_missing_or_incomplete":
            return "UNVERIFIED", str(exc), {}
        return "FAIL", str(exc), {}
    artifacts.write(f"{index:04d}.forwarded.txt", text)
    if not text.strip():
        return "UNVERIFIED", "attachment_content_evidence_missing", {}
    if not rule.get("reference"):
        return "UNVERIFIED", "post_policy_reference_missing", {"forwarded_characters": len(text)}
    assert settings.expectations is not None
    expected = approved_reference(settings, rule).decode("utf-8")
    comparison = differences(normalized(expected), normalized(text))
    details = {
        "comparison": comparison,
        "reference": rule["reference"],
        "reference_sha256": rule["sha256"],
    }
    return (
        ("PASS", "forwarded_content_matches_reference", details)
        if comparison["matches"]
        else ("FAIL", "forwarded_content_mismatch", details)
    )


def rejection(reply: Reply, rule: dict[str, Any]) -> tuple[str, str]:
    if reply.status == rule["http_status"] and reason_code(reply.body) == rule["reason"]:
        return "EXPECTED_REJECTION", "status_and_reason_match"
    return "FAIL", "expected_rejection_did_not_match"


async def run(settings: Settings, corpus: Corpus | None = None) -> tuple[dict[str, Any], int]:
    corpus = corpus or Corpus()
    inventory = corpus.verify()
    if settings.case_ids is not None and (
        not settings.case_ids or set(settings.case_ids) - {case.path for case in corpus.cases}
    ):
        raise ValueError("Empty or unknown case selection")
    rules = overrides(settings.expectations, corpus.cases)
    cases = [
        case
        for case in corpus.cases
        if (not settings.formats or case.format in settings.formats)
        and (settings.case_ids is None or case.path in settings.case_ids)
        and fnmatch.fnmatchcase(case.path, settings.match)
    ]
    if not cases:
        raise ValueError("No inputs matched; no requests sent")
    artifacts = Artifacts(settings.output, (settings.key, settings.capture_key))
    report: dict[str, Any] = {
        "schema_version": 1,
        "tester_version": __version__,
        "inventory": inventory,
        "configuration": {
            "mode": settings.mode,
            "endpoint": settings.url,
            "timeout_seconds": settings.timeout,
            "runtime_label": settings.runtime_label,
            "skip_audio": settings.skip_audio,
            "document_concurrency": 1,
            "document_timeout_seconds": settings.document_timeout,
            "declared_server_page_limits": {
                "direct_per_document": settings.direct_page_limit,
                "gateway_cumulative_pdf": settings.gateway_page_limit,
            },
            "timing_scope": "HTTP upload, service queue, conversion, polling and download",
            "reference_profile": settings.reference_profile,
            "selection": {"formats": settings.formats, "match": settings.match},
            "expectations_sha256": digest(settings.expectations)
            if settings.expectations is not None
            else None,
            "options": settings.options()
            if settings.mode == "direct"
            else {"model": settings.model},
            "capture_configured": bool(settings.capture_url),
        },
        "selected_inputs": len(cases),
        "entire_corpus_selected": len(cases) == len(corpus.cases),
        "results": [],
        "complete": False,
    }
    started = time.perf_counter()
    current = 0
    interrupted = False
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30), follow_redirects=False, trust_env=False
        ) as client:
            transport = Transport(client, settings.max_response_bytes)
            direct = Direct(transport, settings.url, settings.key, settings.options())
            for index, case in enumerate(cases, 1):
                current = index - 1
                rule = expectation(case, settings, rules)
                result: dict[str, Any] = {
                    "case": case.path,
                    "format": case.format,
                    "expected": rule["outcome"],
                    "effective_expectation": rule,
                    "status": "ERROR",
                    "notes": case.notes,
                    "trace": {},
                }
                trace = result["trace"]
                skip_reason = case.skip_reason or (
                    "audio_disabled" if settings.skip_audio and case.family == "audio" else None
                )
                if skip_reason is not None:
                    result.update(
                        status="SKIPPED",
                        reason=skip_reason,
                        expected="skip",
                        effective_expectation={"outcome": "skip", "reason": skip_reason},
                    )
                    report["results"].append(result)
                    current = index
                    artifacts.json("report.json", report)
                    print(f"{index}/{len(cases)} SKIPPED {case.path} ({skip_reason})", flush=True)
                    continue
                correlation = uuid.uuid4().hex
                case_started = time.perf_counter()
                try:
                    data = contained(corpus.root, case.path).read_bytes()
                    async with asyncio.timeout(settings.timeout):
                        if settings.mode == "direct":
                            reply = await direct.convert(case, data, trace)
                        else:
                            trace.update(correlation=correlation, remote_job_may_continue=True)
                            reply = await transport.request(
                                "POST",
                                settings.url + "/chat/completions",
                                headers={settings.auth_header: f"Bearer {settings.key}"}
                                if settings.key
                                else {},
                                json=gateway_payload(case, data, settings.model, correlation),
                            )
                            trace.update(
                                gateway_seconds=reply.seconds, remote_job_may_continue=False
                            )
                            if 200 <= reply.status < 300 and (
                                not isinstance(reply.body, dict)
                                or "error" in reply.body
                                or not isinstance(reply.body.get("choices"), list)
                                or not reply.body["choices"]
                            ):
                                raise ProtocolError("invalid_gateway_completion")
                        result["http_status"] = reply.status
                        artifacts.json(f"{index:04d}.response.json", reply.body)
                        if rule["outcome"] == "reject":
                            result["status"], result["reason"] = rejection(reply, rule)
                        elif not 200 <= reply.status < 300:
                            result.update(status="FAIL", reason="unexpected_http_status")
                        elif settings.mode == "direct":
                            status, reason, detail = check_direct(
                                corpus, case, settings, reply.body, rule
                            )
                            result.update(status=status, reason=reason, **detail)
                        else:
                            status, reason, detail = await check_gateway(
                                transport, settings, correlation, rule, artifacts, index
                            )
                            result.update(status=status, reason=reason, **detail)
                except TimeoutError:
                    result.update(status="ERROR", reason="absolute_deadline_exceeded")
                except httpx.HTTPError:
                    result.update(status="ERROR", reason="transport_error")
                except ProtocolError as exc:
                    result.update(status="FAIL", reason=str(exc))
                except ValueError:
                    result.update(status="FAIL", reason="invalid_document_or_reference")
                except asyncio.CancelledError:
                    interrupted = True
                    result.update(status="ERROR", reason="interrupted")
                if "last_response" in trace:
                    artifacts.json(f"{index:04d}.response.json", trace.pop("last_response"))
                    result["http_status"] = trace["http_status"]
                if (label := failure_label(result)) is not None:
                    result["failure_label"] = label
                result["elapsed_seconds"] = time.perf_counter() - case_started
                report["results"].append(result)
                current = index
                artifacts.json("report.json", report)
                print(f"{index}/{len(cases)} {result['status']} {case.path}", flush=True)
                if interrupted or trace.get("remote_job_may_continue"):
                    report["stopped_reason"] = (
                        "interrupted" if interrupted else "remote_completion_unknown"
                    )
                    break
    except asyncio.CancelledError:
        interrupted = True
        report["stopped_reason"] = "interrupted"
    finally:
        for case in cases[current:]:
            report["results"].append(
                {
                    "case": case.path,
                    "format": case.format,
                    "status": "NOT_RUN",
                    "reason": report.get("stopped_reason", "run_aborted"),
                }
            )
        report["elapsed_seconds"] = time.perf_counter() - started
        report["counts"] = dict(Counter(result["status"] for result in report["results"]))
        report["complete"] = report["counts"].get("NOT_RUN", 0) == 0 and not interrupted
        pages = sum(result.get("pages", 0) for result in report["results"])
        verified_pages = sum(
            result.get("pages", 0) for result in report["results"] if result["status"] == "PASS"
        )
        report["observed_pages"] = pages
        report["verified_pages"] = verified_pages
        report["observed_pages_per_second"] = pages / report["elapsed_seconds"]
        report["verified_pages_per_second"] = verified_pages / report["elapsed_seconds"]
        latencies = sorted(
            result["elapsed_seconds"] for result in report["results"] if "elapsed_seconds" in result
        )
        report["attempted_inputs_per_second"] = len(latencies) / report["elapsed_seconds"]
        report["latency_seconds"] = {
            "p50": statistics.median(latencies) if latencies else None,
            "p95": latencies[math.ceil(len(latencies) * 0.95) - 1] if latencies else None,
        }
        report["all_selected_expectations_met"] = report["complete"] and all(
            result["status"] in {"PASS", "EXPECTED_REJECTION"} for result in report["results"]
        )
        artifacts.json("report.json", report)
        csv_text = io.StringIO()
        fields = (
            "case",
            "format",
            "expected",
            "status",
            "reason",
            "http_status",
            "elapsed_seconds",
            "pages",
        )
        writer = csv.DictWriter(csv_text, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report["results"])
        artifacts.write("report.csv", csv_text.getvalue())
    if interrupted:
        return report, 130
    if report["counts"].get("FAIL", 0) or report["counts"].get("ERROR", 0):
        return report, 1
    return report, 0 if report["all_selected_expectations_met"] else 2
