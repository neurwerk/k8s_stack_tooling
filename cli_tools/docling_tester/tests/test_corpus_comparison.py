from __future__ import annotations

import copy
import json

import pytest
from conftest import document, tiny_corpus

from docling_tester.compare import Document, compare_documents
from docling_tester.corpus import Corpus


def test_complete_pinned_snapshot_is_accounted_and_unchanged():
    corpus = Corpus()
    result = corpus.verify()
    assert result["inputs"] == len(corpus.cases)
    assert result["revision"] == "0563d23e004f403b61e181fe3a743be06894d52e"
    supplemental = {path for path in corpus.files if path.startswith("supplemental/")}
    assert supplemental == {"supplemental/tensor-network-202-pages.pdf"}
    assert len(corpus.files.keys() - supplemental) == 1366  # Complete upstream snapshot.
    assert result["files"] == 1367
    assert {case.format for case in corpus.cases} >= {"pdf", "xlsx", "xlsm", "docx", "pptx"}


@pytest.mark.parametrize("damage", ["missing", "changed", "new", "duplicate", "reference"])
def test_inventory_never_accepts_missing_changed_or_unmapped_data(tmp_path, damage):
    corpus = tiny_corpus(tmp_path)
    path = tmp_path / "manifest.json"
    if damage == "missing":
        (corpus.root / "sample.xlsx").unlink()
    elif damage == "changed":
        (corpus.root / "sample.xlsx").write_bytes(b"wrong")
    elif damage == "new":
        (corpus.root / "new.pdf").write_bytes(b"unmapped")
    elif damage == "duplicate":
        manifest = json.loads(path.read_text())
        manifest["cases"].append(manifest["cases"][0])
        path.write_text(json.dumps(manifest))
    else:
        (corpus.root / "sample.xlsx.json").unlink()
    with pytest.raises(ValueError):
        Corpus(corpus.root, path).verify()


@pytest.mark.parametrize(
    "damage", ["page", "paragraph", "row", "number", "swapped_cells", "order", "figure", "sheet"]
)
def test_successful_but_incomplete_content_fails(damage):
    expected, actual = document(), document()
    cells = actual["tables"][0]["data"]["table_cells"]
    children = actual["groups"][0]["children"]
    if damage == "page":
        del actual["pages"]["2"]
    elif damage == "paragraph":
        actual["texts"].pop()
        children.pop(1)
    elif damage == "row":
        actual["tables"][0]["data"]["num_rows"] = 1
        del cells[2:]
    elif damage == "number":
        cells[1]["text"] = "100"
    elif damage == "swapped_cells":
        cells[0]["text"], cells[1]["text"] = cells[1]["text"], cells[0]["text"]
    elif damage == "order":
        children[0], children[1] = children[1], children[0]
    elif damage == "figure":
        actual["pictures"].clear()
        children.pop()
    else:
        actual["groups"][0]["name"] = "Other sheet"
    try:
        comparison = compare_documents(expected, actual)
    except ValueError:
        return  # Invalid graph/page references are a failure, never a partial PASS.
    assert comparison["matches"] is False


def test_normalization_keeps_content_but_ignores_layout_noise():
    expected, actual = document(), document()
    actual["texts"][1].update(text="  Second\n page  ", orig="old metadata", confidence=0.8)
    actual["texts"][1]["prov"][0]["bbox"] = {"x": 50}
    actual["origin"] = {"filename": "different-generated-name"}
    assert compare_documents(expected, actual)["matches"]


def test_rich_merged_cells_and_broken_references():
    expected = document()
    expected["texts"].append({"text": "Rich 001", "label": "text", "prov": [{"page_no": 1}]})
    cells = expected["tables"][0]["data"]["table_cells"]
    cells[0].update(text="", ref={"$ref": "#/texts/2"}, end_col_offset_idx=2)
    cells.pop(1)
    actual = copy.deepcopy(expected)
    assert compare_documents(expected, actual)["matches"]
    actual["texts"][2]["text"] = "Rich 100"
    assert not compare_documents(expected, actual)["matches"]
    cells[0]["ref"] = {"$ref": "#/texts/999"}
    with pytest.raises(ValueError, match="Unresolved"):
        Document(expected).canonical()


def test_cycle_and_orphan_content_cannot_match():
    value = document()
    value["groups"][0]["children"].append({"$ref": "#/groups/0"})
    with pytest.raises(ValueError, match="Cyclic"):
        Document(value).canonical()
    value = document()
    value["texts"].append({"text": "Invisible content", "label": "text"})
    with pytest.raises(ValueError, match="Unreachable"):
        Document(value).canonical()
