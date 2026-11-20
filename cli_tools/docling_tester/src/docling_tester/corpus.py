"""Read a complete, immutable, independently hashed upstream snapshot."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PACKAGE = Path(__file__).parent
GATEWAY_FILES = {"pdf", "docx", "xlsx", "pptx", "txt", "md", "csv"}
GATEWAY_IMAGES = {"png", "jpg", "jpeg", "webp"}


@dataclass(frozen=True)
class Case:
    path: str
    family: str
    format: str
    references: tuple[dict[str, str], ...]
    notes: tuple[str, ...]


def contained(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or Path(name).is_absolute():
        raise ValueError("Path must stay within its data directory")
    return path


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Corpus:
    def __init__(self, root: Path = PACKAGE / "upstream", manifest: Path = PACKAGE / "corpus.json"):
        self.root = root
        self.manifest: dict[str, Any] = json.loads(manifest.read_text())
        if self.manifest.get("schema_version") != 1:
            raise ValueError("Unsupported corpus manifest")
        files = self.manifest["files"]
        self.files = {entry["path"]: entry for entry in files}
        if len(files) != len(self.files):
            raise ValueError("Duplicate corpus file")
        self.cases = [
            Case(
                entry["path"],
                entry["family"],
                entry["format"],
                tuple(entry["references"]),
                tuple(entry["notes"]),
            )
            for entry in self.manifest["cases"]
        ]
        inputs = {p for p, entry in self.files.items() if entry["role"] == "input"}
        if len({case.path for case in self.cases}) != len(self.cases):
            raise ValueError("Duplicate input case")
        if inputs != {case.path for case in self.cases}:
            raise ValueError("Every input must have exactly one case")
        for case in self.cases:
            for reference in case.references:
                entry = self.files.get(reference["path"], {})
                if entry.get("role") != "expected":
                    raise ValueError("Undeclared reference")

    def verify(self) -> dict[str, Any]:
        actual = {
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*")
            if p.is_file() and not (p.suffix == ".pyc" and p.parent.name == "__pycache__")
        }
        if actual != set(self.files):
            raise ValueError("Corpus has missing or unaccounted files")
        for name, entry in self.files.items():
            path = contained(self.root, name)
            if (self.root / name).is_symlink() or path.stat().st_size != entry["bytes"]:
                raise ValueError(f"Corpus file size/type mismatch: {name}")
            if digest(path) != entry["sha256"]:
                raise ValueError(f"Corpus checksum mismatch: {name}")
        return {
            "revision": self.manifest["revision"],
            "files": len(self.files),
            "bytes": sum(entry["bytes"] for entry in self.files.values()),
            "roles": dict(Counter(entry["role"] for entry in self.files.values())),
            "inputs": len(self.cases),
            "formats": dict(sorted(Counter(case.format for case in self.cases).items())),
            "inputs_with_json_references": sum(bool(case.references) for case in self.cases),
        }

    def read_document(self, reference: dict[str, str]) -> dict[str, Any]:
        data = contained(self.root, reference["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != self.files[reference["path"]]["sha256"]:
            raise ValueError("Reference checksum changed")
        value = json.loads(data)
        if not isinstance(value, dict) or value.get("schema_name") != "DoclingDocument":
            raise ValueError("Reference is not a DoclingDocument")
        return value

    def markdown_reference(self, case: Case) -> str | None:
        # Upstream test_backend_asciidoc.test_asciidocs_examples uses this
        # exact export contract; other formats need their own compatibility review.
        if case.format != "asciidoc":
            return None
        source = Path(case.path)
        path = (source.parent.parent / "groundtruth" / (source.name + ".md")).as_posix()
        if self.files.get(path, {}).get("role") != "expected":
            return None
        return path

    def read_markdown(self, path: str) -> str:
        data = contained(self.root, path).read_bytes()
        if hashlib.sha256(data).hexdigest() != self.files[path]["sha256"]:
            raise ValueError("Reference checksum changed")
        return data.decode("utf-8")


def reference_for(case: Case, profile: str) -> dict[str, str] | None:
    return next((ref for ref in case.references if ref["profile"] == profile), None)
