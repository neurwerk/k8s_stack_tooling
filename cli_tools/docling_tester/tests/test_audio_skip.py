import asyncio
from dataclasses import replace

import pytest
from conftest import document, tiny_corpus

from docling_tester.runner import Settings, run


@pytest.mark.parametrize("skip_audio", [False, True])
def test_audio_skip_does_not_upload_or_block_following_document(tmp_path, http_server, skip_audio):
    corpus = tiny_corpus(tmp_path, ("sound.mp3", "sheet.xlsx"))
    corpus.cases[0] = replace(corpus.cases[0], family="audio")
    uploads = []

    def callback(method, path, raw, headers):
        if method == "POST":
            uploads.append(raw)
            return 200, {"task_id": "job", "task_type": "convert", "task_status": "success"}
        return 200, {"status": "success", "errors": [], "document": {"json_content": document()}}

    report, code = asyncio.run(
        run(
            Settings("direct", http_server(callback), tmp_path / "results", skip_audio=skip_audio),
            corpus,
        )
    )
    assert len(uploads) == (1 if skip_audio else 2)
    assert len(report["results"]) == 2
    assert report["results"][1]["status"] == "PASS"
    if skip_audio:
        skipped = report["results"][0]
        assert skipped["status"] == "SKIPPED"
        assert skipped["reason"] == "audio_disabled"
        assert "elapsed_seconds" not in skipped
        assert report["counts"] == {"SKIPPED": 1, "PASS": 1}
        assert report["all_selected_expectations_met"] is False
        assert code == 2
    else:
        assert report["counts"] == {"PASS": 2}
        assert code == 0
