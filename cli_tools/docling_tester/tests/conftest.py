from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, override

import pytest

from docling_tester.corpus import Corpus, digest


def document() -> dict[str, Any]:
    def cell(row: int, col: int, text: str) -> dict[str, Any]:
        return {
            "start_row_offset_idx": row,
            "end_row_offset_idx": row + 1,
            "start_col_offset_idx": col,
            "end_col_offset_idx": col + 1,
            "text": text,
            "column_header": False,
            "row_header": False,
            "row_section": False,
        }

    return {
        "schema_name": "DoclingDocument",
        "version": "1.10.0",
        "pages": {"1": {"page_no": 1}, "2": {"page_no": 2}},
        "body": {"children": [{"$ref": "#/groups/0"}]},
        "furniture": {"children": []},
        "groups": [
            {
                "label": "sheet",
                "name": "Inventory",
                "children": [
                    {"$ref": "#/texts/0"},
                    {"$ref": "#/texts/1"},
                    {"$ref": "#/tables/0"},
                    {"$ref": "#/pictures/0"},
                ],
            }
        ],
        "texts": [
            {"label": "title", "text": "Inventory", "prov": [{"page_no": 1}]},
            {"label": "text", "text": "Second page", "prov": [{"page_no": 2}]},
        ],
        "pictures": [{"label": "picture", "prov": [{"page_no": 2}], "children": []}],
        "tables": [
            {
                "label": "table",
                "prov": [{"page_no": 1}],
                "data": {
                    "num_rows": 2,
                    "num_cols": 2,
                    "table_cells": [
                        cell(0, 0, "Alpha"),
                        cell(0, 1, "10"),
                        cell(1, 0, "Beta"),
                        cell(1, 1, "20"),
                    ],
                },
            }
        ],
    }


def tiny_corpus(parent: Path, names: tuple[str, ...] = ("sample.xlsx",)) -> Corpus:
    root = parent / "corpus"
    root.mkdir()
    files, cases = [], []
    for name in names:
        source = root / name
        source.write_bytes(b"PK\x03\x04\x00fixture\xff" + name.encode())
        ref = root / (name + ".json")
        ref.write_text(json.dumps(document()))
        for file, role in ((source, "input"), (ref, "expected")):
            files.append(
                {
                    "path": file.name,
                    "role": role,
                    "sha256": digest(file),
                    "bytes": file.stat().st_size,
                }
            )
        cases.append(
            {
                "path": name,
                "family": "fixtures",
                "format": name.rsplit(".", 1)[1],
                "references": [{"path": ref.name, "profile": "native"}],
                "notes": [],
            }
        )
    manifest = parent / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": "fixture",
                "files": files,
                "cases": cases,
            }
        )
    )
    return Corpus(root, manifest)


Callback = Callable[[str, str, bytes, dict[str, str]], tuple[int, Any]]


@pytest.fixture
def http_server() -> Iterator[Callable[[Callback], str]]:
    servers: list[tuple[ThreadingHTTPServer, threading.Thread]] = []

    def start(callback: Callback) -> str:
        class Handler(BaseHTTPRequestHandler):
            @override
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def handle_request(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                status, value = callback(self.command, self.path, body, dict(self.headers))
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = handle_request
            do_POST = handle_request

        backend = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=backend.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        servers.append((backend, thread))
        return f"http://127.0.0.1:{backend.server_port}"

    yield start
    for backend, thread in servers:
        backend.shutdown()
        backend.server_close()
        thread.join()
