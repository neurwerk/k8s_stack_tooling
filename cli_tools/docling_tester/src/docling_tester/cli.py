"""CLI help is the usage guide; commands never discover credentials or deploy services."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from docling_tester import __version__
from docling_tester.capture import server
from docling_tester.corpus import Corpus
from docling_tester.runner import Settings, run

EXPECTATIONS_HELP = """
Expectations are independently reviewed JSON/text files, never generated from a live response.
Pass --expectations /path/to/expectations.json. Paths below are relative to that file.
Use exact case IDs from `docling-tester inventory --list`; omitted cases use shipped defaults.

{
  "schema_version": 1,
  "cases": {
    "tests/data/csv/sources/csv-comma.csv": {
      "gateway": {
        "outcome": "convert",
        "reference": "csv-comma.forwarded.txt",
        "sha256": "<SHA-256 of independently reviewed post-policy text>"
      }
    },
    "tests/data/xlsx/sources/xlsx_02_sample_sales_data.xlsm": {
      "gateway": {
        "outcome": "reject", "http_status": 400, "reason": "unsupported_format"
      }
    }
  }
}

A direct conversion override uses outcome=convert, reference=<DoclingDocument JSON>,
sha256=<digest>, and profile=<matching --reference-profile>. A rejection always needs
both its 4xx status AND exact machine-readable reason/code. Server errors are never
expected rejections. Shipped Gateway format expectations describe the current platform
allowlist; use overrides when testing a different reviewed contract.

PASS: content matches the selected independent reference.
EXPECTED_REJECTION: status AND reason match; no claim of successful extraction.
FAIL: contradictory content, protocol failure, or unexpected status/reason.
UNVERIFIED: insufficient evidence (no compatible reference or backend capture).
ERROR: transport failure or timeout. NOT_RUN: interrupted/stopped before completion.
Exit 0: all selected expectations met. Exit 1: failure/error. Exit 2: incomplete or
unverified. Exit 130: interrupted. An empty selection is an error.

Direct native-format references apply to Office and other non-visual converters.
PDF standard-no-ocr references require --pipeline standard --no-ocr. OCR references
use profiles such as ocr:rapidocr:pdf_aware_layout_regions. --reference-profile is an
explicit operator assertion that the service options/version are compatible.
Record exact service/model versions and page concurrency with --runtime-label.
No reference is automatically generated or updated. Coordinates, confidence, IDs,
styling, and whitespace differences are ignored; text, numeric values, reading order,
page provenance, sheets/slides, figures and table structure are compared.

Gateway verification needs a separately approved binding to the capture backend.
Set different DOCLING_TESTER_CAPTURE_BACKEND_KEY and DOCLING_TESTER_CAPTURE_READ_KEY
environment variables, then run `docling-tester serve-capture`. Configure the test
binding's upstream Authorization Bearer credential with the backend key. Give the
runner --capture-url <private-capture-base-url>; it retrieves evidence directly with
the reader key. An LLM answer/echo is NEVER extraction evidence (Gateway can restore
masked text in responses). Captures are in memory, expire after 10 minutes, and are
consumed on retrieval. Each request has a unique correlation ID. Golden Gateway
text must describe the complete forwarded user text after the chosen PII policy,
including the Document filename header and any policy notice. Unknown masking
behavior remains UNVERIFIED until that post-policy reference is reviewed.

Run artifacts contain fixture responses and differences in a private output directory.
The output directory must not already exist. Credentials come only from environment
variables and are redacted from saved responses. TLS verification stays enabled.
Runs submit one document at a time over a persistent client; timing includes transfer,
service queueing, conversion and download. It is not isolated GPU decode time.
A lost submission/timeout can leave remote conversion running; the run stops rather
than adding more work. It does not cancel/reconfigure the remote service.
"""


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Test all pinned Docling fixture inputs through Docling or AgentGateway.",
        epilog="Run `docling-tester expectations-help` for reference and capture setup details.",
    )
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    menu = commands.add_parser("menu", help="Interactive tests and private JSONC settings")
    menu.add_argument("--config", type=Path, default=Path("settings.local.jsonc"))
    inventory = commands.add_parser("inventory", help="Verify hashes and account for every file")
    inventory.add_argument("--list", action="store_true", help="List every input and its profiles")
    commands.add_parser(
        "expectations-help", help="Show reference schema, results and capture setup"
    )
    test = commands.add_parser(
        "run",
        help="Run the complete corpus or an explicit subset",
        epilog=(
            "Direct targets Docling's conversion service, not a raw vLLM image endpoint. "
            "Gateway URL is the binding base ending in /v1. "
            "Default selection is every input, including expected unsupported-format probes."
        ),
    )
    test.add_argument("mode", choices=("direct", "gateway"))
    test.add_argument("--url", required=True, help="Explicit service/binding base URL")
    test.add_argument("--output", type=Path, required=True, help="New private artifact directory")
    test.add_argument(
        "--key-env",
        help="Credential environment variable (default DOCLING_API_KEY/GATEWAY_API_KEY)",
    )
    test.add_argument("--timeout", type=float, default=300, help="Absolute seconds per input (300)")
    test.add_argument("--pipeline", choices=("standard", "native", "vlm"), default="standard")
    test.add_argument("--ocr", action=argparse.BooleanOptionalAction, default=True)
    test.add_argument("--ocr-preset", default="rapidocr")
    test.add_argument("--vlm-preset", default="default", help="Named preset on the Docling service")
    test.add_argument(
        "--reference-profile",
        default="auto",
        help="auto, native, standard-no-ocr, or an explicitly compatible custom/OCR profile",
    )
    test.add_argument(
        "--runtime-label", default="unknown", help="Service/model versions and settings"
    )
    test.add_argument("--model", default="", help="Gateway binding's canonical model ID")
    test.add_argument(
        "--auth-header",
        choices=("Authorization", "Authorization-Neurwerk"),
        default="Authorization",
    )
    test.add_argument("--capture-url", default="", help="Private capture-reader base URL")
    test.add_argument("--capture-key-env", default="DOCLING_TESTER_CAPTURE_READ_KEY")
    test.add_argument("--format", action="append", default=[], help="Extension filter; repeatable")
    test.add_argument("--match", default="*", help="Glob against full case ID (default all)")
    test.add_argument(
        "--expectations", type=Path, help="Reviewed per-case expected outputs/rejections"
    )
    test.add_argument(
        "--max-response-bytes",
        type=int,
        default=64 * 1024 * 1024,
        help="Bound each service response (default 64 MiB)",
    )
    capture = commands.add_parser(
        "serve-capture",
        help="Run the optional private, in-memory test backend",
        epilog="Deploying/routing a Gateway test binding is a separate operator action.",
    )
    capture.add_argument("--host", default="127.0.0.1")
    capture.add_argument("--port", type=int, default=8765)
    capture.add_argument("--backend-key-env", default="DOCLING_TESTER_CAPTURE_BACKEND_KEY")
    capture.add_argument("--read-key-env", default="DOCLING_TESTER_CAPTURE_READ_KEY")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "menu":
            from docling_tester.live import main as menu_main

            return menu_main(["--config", str(args.config)])
        if args.command == "expectations-help":
            print(EXPECTATIONS_HELP.strip())
            return 0
        if args.command == "inventory":
            corpus = Corpus()
            print(json.dumps(corpus.verify(), indent=2))
            if args.list:
                for case in corpus.cases:
                    profiles = ",".join(ref["profile"] for ref in case.references) or "unverified"
                    print(f"{case.format}\t{case.path}\t{profiles}")
            return 0
        if args.command == "serve-capture":
            with server(
                args.host,
                args.port,
                os.environ.get(args.backend_key_env, ""),
                os.environ.get(args.read_key_env, ""),
            ) as backend:
                print(f"Capture backend listening on {args.host}:{backend.server_port}", flush=True)
                backend.serve_forever()
            return 0
        key_env = args.key_env or (
            "DOCLING_API_KEY" if args.mode == "direct" else "GATEWAY_API_KEY"
        )
        settings = Settings(
            mode=args.mode,
            url=args.url,
            output=args.output,
            key=os.environ.get(key_env, ""),
            timeout=args.timeout,
            pipeline=args.pipeline,
            ocr=args.ocr,
            ocr_preset=args.ocr_preset,
            vlm_preset=args.vlm_preset,
            reference_profile=args.reference_profile,
            runtime_label=args.runtime_label,
            model=args.model,
            auth_header=args.auth_header,
            capture_url=args.capture_url,
            capture_key=os.environ.get(args.capture_key_env, ""),
            formats=tuple(args.format),
            match=args.match,
            expectations=args.expectations,
            max_response_bytes=args.max_response_bytes,
        )
        report, code = asyncio.run(run(settings))
        print(
            json.dumps(
                {
                    "counts": report["counts"],
                    "complete": report["complete"],
                    "observed_pages_per_second": report["observed_pages_per_second"],
                    "verified_pages_per_second": report["verified_pages_per_second"],
                },
                indent=2,
            )
        )
        print(f"Report: {settings.output / 'report.json'}")
        return code
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError) as exc:
        # Exception strings can contain a credential supplied by an HTTP peer or URL.
        print(f"Tester could not complete ({type(exc).__name__}); check arguments and input files.")
        return 2
