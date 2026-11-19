"""Import the exact upstream tree and build its explicit file/case inventory."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

REVISION = "0563d23e004f403b61e181fe3a743be06894d52e"
PROJECT = Path(__file__).resolve().parents[1]
PACKAGE = PROJECT / "src/docling_tester"
INPUT_SUFFIXES = {
    ".aac",
    ".afp",
    ".asciidoc",
    ".avi",
    ".boxnote",
    ".csv",
    ".doc",
    ".docx",
    ".ebc",
    ".eml",
    ".epub",
    ".eps",
    ".flac",
    ".gz",
    ".htm",
    ".html",
    ".ics",
    ".jpeg",
    ".jpg",
    ".json",
    ".key",
    ".m4a",
    ".md",
    ".mhtml",
    ".mov",
    ".mp3",
    ".mp4",
    ".msg",
    ".numbers",
    ".nxml",
    ".odp",
    ".ods",
    ".odt",
    ".ogg",
    ".pages",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".rtf",
    ".tex",
    ".tif",
    ".txt",
    ".vtt",
    ".wav",
    ".webp",
    ".xls",
    ".xlsm",
    ".xlsx",
    ".xml",
    ".zip",
}
SIDECARS = {".abs", ".bbl", ".bib", ".bst", ".cls", ".sty", ".xsd"}


def classify(path: Path) -> tuple[str, str]:
    parts = path.parts
    if path.as_posix() == "LICENSE":
        return "support", "Upstream MIT copyright and license"
    if "groundtruth" in parts:
        return "expected", "Upstream checked-in expected output"
    if path.name.lower().startswith("readme."):
        return "support", "Upstream fixture notice"
    if path.suffix == ".py":
        return "support", "Upstream test or fixture generator; never executed by the runner"
    if path.name.endswith((".source.txt", ".source.json")):
        return "expected", "Upstream parser source-text expectation"
    if path.name.endswith(".layout.json") or path.suffix in SIDECARS:
        return "support", "Required layout/schema/LaTeX companion"
    if len(parts) > 2 and parts[:2] == ("tests", "data") and path.suffix in INPUT_SUFFIXES:
        return "input", "Document or parser input, including standalone image/PDF assets"
    raise ValueError(f"Unclassified upstream file: {path}")


def references(root: Path, path: Path) -> list[dict[str, str]]:
    family = path.parts[2]
    gt = root / "tests/data" / family / "groundtruth"
    candidates: list[dict[str, str]] = []
    for file in sorted(gt.rglob("*.json")):
        if file.name.endswith(".pages.meta.json"):
            continue
        relative = file.relative_to(gt)
        if len(relative.parts) == 1:
            if file.name not in {path.name + ".json", path.stem + ".json"}:
                continue
            profile = (
                "standard-no-ocr"
                if path.suffix == ".pdf"
                else "parser-deepseek"
                if family == "md_deepseek"
                else "native"
            )
        else:
            engine, mode = relative.parts[:2]
            if file.name != f"{path.stem}.{engine}.{mode}.json":
                continue
            profile = f"ocr:{engine}:{mode}"
        data = json.loads(file.read_text())
        if isinstance(data, dict) and data.get("schema_name") == "DoclingDocument":
            candidates.append({"path": file.relative_to(root).as_posix(), "profile": profile})
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    args = parser.parse_args()
    root = PACKAGE / "upstream"
    if root.exists():
        parser.error("Snapshot already exists; imports never overwrite a snapshot")
    archive = subprocess.check_output(
        ["git", "archive", REVISION, "LICENSE", "tests"], cwd=args.checkout
    )
    root.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(root, filter="data")
    files, cases = [], []
    for file in sorted(root.rglob("*")):
        if not file.is_file():
            continue
        path = file.relative_to(root)
        role, reason = classify(path)
        data = file.read_bytes()
        files.append(
            {
                "path": path.as_posix(),
                "role": role,
                "reason": reason,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
        if role == "input":
            refs = references(root, path)
            notes = [] if refs else ["No compatible structured upstream reference is available."]
            if path.parts[2] in {"json_dots", "json_paddleocr_vl", "html_chandra", "md_deepseek"}:
                notes.append("Parser fixture; conversion-service support must be measured.")
            if path.parts[2] in {"latex", "ebcdic", "xbrl"}:
                notes.append("Input may require companion assets or backend-specific options.")
            cases.append(
                {
                    "path": path.as_posix(),
                    "family": path.parts[2],
                    "format": path.suffix.lstrip("."),
                    "references": refs,
                    "notes": notes,
                }
            )
    manifest = {
        "schema_version": 1,
        "repository": "https://github.com/docling-project/docling",
        "revision": REVISION,
        "copyright": "The Docling Contributors",
        "license": "MIT; complete upstream notice in upstream/LICENSE",
        "files": files,
        "cases": cases,
    }
    (PACKAGE / "corpus.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Imported {len(files)} files, including {len(cases)} inputs, at {REVISION}")


if __name__ == "__main__":
    main()
