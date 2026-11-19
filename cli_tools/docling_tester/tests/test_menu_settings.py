import asyncio
import json

import pytest
from conftest import document, tiny_corpus

from docling_tester import live, settings
from docling_tester.runner import Settings, run


def test_jsonc_preserves_strings_and_private_atomic_save(tmp_path, monkeypatch):
    path = tmp_path / "settings.local.jsonc"
    values = settings.DEFAULTS | {
        "direct_url": "https://example.com/path//value",
        "direct_api_key": 'key"with\\escape/*literal*/',
    }
    path.write_text("// comment\n" + json.dumps(values) + "\n/* trailing comment */")
    assert settings.load(path) == values
    settings.save(path, values)
    assert path.stat().st_mode & 0o777 == 0o600
    settings.save(path, values)
    assert settings.load(path) == values
    previous = path.read_bytes()

    def fail(*args):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(settings.os, "replace", fail)
    with pytest.raises(OSError):
        settings.save(path, values | {"direct_api_key": "new-secret"})
    assert path.read_bytes() == previous
    assert not list(tmp_path.glob(".settings-*.tmp"))


@pytest.mark.parametrize("value", ['{"timeout":true}', '{"unknown":"secret"}', '{"direct_url":'])
def test_invalid_settings_fail_without_echoing_values(tmp_path, value):
    path = tmp_path / "settings.local.jsonc"
    path.write_text(value)
    with pytest.raises(ValueError, match="Invalid JSONC settings"):
        settings.load(path)


def test_menu_hides_keys_and_maps_selected_cases(tmp_path, monkeypatch):
    runner = live.Runner(tmp_path / "settings.local.jsonc")
    runner.settings.update(
        direct_url="http://example.invalid",
        direct_api_key="secret-sentinel",
        capture_api_key="capture-sentinel",
    )
    displayed = []

    def back(title, options):
        displayed.extend(options)
        return len(options) - 1

    monkeypatch.setattr(live, "choose", back)
    runner.configure()
    assert "secret-sentinel" not in str(displayed)
    assert "capture-sentinel" not in str(displayed)
    captured = []

    async def fake_run(configuration, corpus):
        captured.append(configuration)
        return {"results": [], "counts": {}}, 0

    monkeypatch.setattr(live, "choose", lambda *args: 0)
    monkeypatch.setattr(live, "run", fake_run)
    runner.launch(runner.corpus.cases[:2])
    assert captured[0].case_ids == tuple(case.path for case in runner.corpus.cases[:2])
    assert captured[0].key == "secret-sentinel"
    assert captured[0].capture_key == "capture-sentinel"


def test_exact_selection_does_not_expand_glob_characters(tmp_path, http_server):
    corpus = tiny_corpus(tmp_path, ("a[1].xlsx", "a1.xlsx", "b.xlsx"))
    url = http_server(
        lambda method, *args: (
            200,
            {
                "task_id": "job",
                "task_type": "convert",
                "task_status": "success",
            }
            if method == "POST"
            else {
                "status": "success",
                "errors": [],
                "document": {"json_content": document()},
            },
        )
    )
    configuration = Settings("direct", url, tmp_path / "out", case_ids=("a[1].xlsx", "b.xlsx"))
    report, code = asyncio.run(run(configuration, corpus))
    assert code == 0
    assert [item["case"] for item in report["results"]] == ["a[1].xlsx", "b.xlsx"]
    for selection in ((), ("unknown.xlsx",)):
        configuration.case_ids = selection
        with pytest.raises(ValueError, match="selection"):
            asyncio.run(run(configuration, corpus))
