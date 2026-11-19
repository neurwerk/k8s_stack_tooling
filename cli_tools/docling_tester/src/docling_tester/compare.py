"""Strict content checks independent of Docling and the production projector.

Ignore coordinates, confidence, generated IDs and styling. Preserve text order,
page provenance, sheet/slide names, figures, cell positions, spans and headers.
Only Unicode NFC and runs of whitespace are normalized; case/numbers remain exact.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

COLLECTIONS = (
    "texts",
    "groups",
    "tables",
    "pictures",
    "key_value_items",
    "form_items",
    "field_regions",
    "field_items",
)


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def object_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value


def list_value(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("Expected JSON array")
    return value


def integer(value: Any) -> int:
    if type(value) is not int:
        raise ValueError("Expected integer")
    return value


class Document:
    def __init__(self, value: Any):
        self.value = object_value(value)
        if self.value.get("schema_name") != "DoclingDocument":
            raise ValueError("Missing DoclingDocument schema")
        self.nodes: dict[str, dict[str, Any]] = {}
        for kind in COLLECTIONS:
            for index, node in enumerate(list_value(self.value.get(kind, []))):
                self.nodes[f"#/{kind}/{index}"] = object_value(node)
        self.pages = object_value(self.value.get("pages"))
        if any(
            not key.isdigit() or int(key) < 1 or object_value(page).get("page_no") != int(key)
            for key, page in self.pages.items()
        ):
            raise ValueError("Invalid page identity")
        if self.pages and set(self.pages) != {str(i) for i in range(1, len(self.pages) + 1)}:
            raise ValueError("Non-contiguous pages")
        self.seen: set[str] = set()
        self.steps = 0

    def _reference(self, reference: Any) -> str:
        obj = object_value(reference)
        pointer = obj.get("$ref")
        if set(obj) != {"$ref"} or not isinstance(pointer, str) or pointer not in self.nodes:
            raise ValueError("Unresolved document reference")
        return pointer

    def _pages(self, node: dict[str, Any]) -> list[int]:
        result = []
        for provenance in list_value(node.get("prov", [])):
            page = object_value(provenance).get("page_no")
            if type(page) is not int or str(page) not in self.pages:
                raise ValueError("Invalid content page provenance")
            result.append(page)
        return sorted(set(result))

    def _cell(self, cell: Any, rows: int, cols: int, active: frozenset[str]) -> dict[str, Any]:
        value = object_value(cell)
        names = (
            "start_row_offset_idx",
            "end_row_offset_idx",
            "start_col_offset_idx",
            "end_col_offset_idx",
        )
        offsets = [integer(value.get(key)) for key in names]
        sr, er, sc, ec = offsets
        if not (0 <= sr < er <= rows and 0 <= sc < ec <= cols):
            raise ValueError("Out-of-range table cell")
        text = value.get("text", "")
        if not isinstance(text, str):
            raise ValueError("Invalid cell text")
        result = {
            "position": offsets,
            "text": normalized(text),
            "headers": [
                value.get(name, False) for name in ("column_header", "row_header", "row_section")
            ],
        }
        if "ref" in value:
            result["rich_content"] = self._walk(value["ref"], active)
        return result

    def _walk(self, reference: Any, active: frozenset[str]) -> dict[str, Any]:
        pointer = self._reference(reference)
        self.steps += 1
        if pointer in active or len(active) > 100 or self.steps > 200_000:
            raise ValueError("Cyclic or excessively expanded document")
        self.seen.add(pointer)
        active = active | {pointer}
        node = self.nodes[pointer]
        result: dict[str, Any] = {
            "kind": pointer.split("/")[1],
            "label": node.get("label"),
            "pages": self._pages(node),
        }
        # Keep additional semantic fields conservatively, including graphs, comments,
        # list numbering and visibility. Unknown fields are compared rather than dropped.
        ignored = {
            "self_ref",
            "parent",
            "prov",
            "orig",
            "formatting",
            "image",
            "confidence",
            "label",
            "text",
            "name",
            "data",
            "children",
            "captions",
            "footnotes",
            "references",
            "annotations",
        }
        result["semantics"] = {key: value for key, value in node.items() if key not in ignored}
        if node.get("annotations"):
            result["annotations"] = node["annotations"]
        if "text" in node:
            if not isinstance(node["text"], str):
                raise ValueError("Invalid document text")
            result["text"] = normalized(node["text"])
        if result["kind"] == "groups":
            result["name"] = node.get("name", "")
        if result["kind"] == "tables":
            data = object_value(node.get("data"))
            rows, cols = data.get("num_rows"), data.get("num_cols")
            if (
                type(rows) is not int
                or type(cols) is not int
                or rows < 0
                or cols < 0
                or (rows == 0) != (cols == 0)
            ):
                raise ValueError("Invalid table size")
            cells = [
                self._cell(cell, rows, cols, active) for cell in list_value(data.get("table_cells"))
            ]
            occupied: set[tuple[int, int]] = set()
            if rows * cols > 1_000_000:
                raise ValueError("Table comparison limit")
            for cell in cells:
                sr, er, sc, ec = cell["position"]
                for row in range(sr, er):
                    for col in range(sc, ec):
                        if (row, col) in occupied:
                            raise ValueError("Overlapping table cells")
                        occupied.add((row, col))
            result["table"] = {
                "rows": rows,
                "cols": cols,
                "cells": sorted(cells, key=lambda cell: cell["position"]),
            }
        for field in ("children", "captions", "footnotes", "references"):
            if node.get(field):
                result[field] = [self._walk(ref, active) for ref in list_value(node[field])]
        # Descriptions are content; embedded pixel buffers and classification scores are not.
        if result["kind"] == "pictures":
            result["descriptions"] = [
                normalized(item["text"])
                for item in list_value(node.get("annotations", []))
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
        return result

    def canonical(self) -> dict[str, Any]:
        result: dict[str, Any] = {"pages": sorted(map(int, self.pages))}
        for root in ("body", "furniture"):
            node = object_value(self.value.get(root))
            result[root] = [
                self._walk(ref, frozenset()) for ref in list_value(node.get("children"))
            ]
        if self.seen != set(self.nodes):
            raise ValueError("Unreachable content in document")
        return result


def differences(expected: Any, actual: Any, limit: int = 80) -> dict[str, Any]:
    """Store structural locations/counts, not source text, in the summary report."""
    changes: list[dict[str, Any]] = []
    count = 0

    def walk(left: Any, right: Any, path: str) -> None:
        nonlocal count
        if left == right:
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                walk(left.get(key), right.get(key), f"{path}/{key}")
        elif isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                record(f"{path}/length", len(left), len(right))
            for index, (a, b) in enumerate(zip(left, right, strict=False)):
                walk(a, b, f"{path}/{index}")
        else:
            record(path, left, right)

    def record(path: str, left: Any, right: Any) -> None:
        nonlocal count
        count += 1
        if len(changes) < limit:
            changes.append(
                {
                    "path": path,
                    "expected_sha256": hashlib.sha256(json.dumps(left).encode()).hexdigest(),
                    "actual_sha256": hashlib.sha256(json.dumps(right).encode()).hexdigest(),
                }
            )

    walk(expected, actual, "")
    return {"matches": count == 0, "difference_count": count, "differences": changes}


def compare_documents(expected: Any, actual: Any) -> dict[str, Any]:
    left, right = Document(expected), Document(actual)
    result = differences(left.canonical(), right.canonical())
    result.update(
        expected_pages=len(left.pages),
        actual_pages=len(right.pages),
        expected_schema=left.value.get("version"),
        actual_schema=right.value.get("version"),
    )
    return result
