import json
from unittest.mock import Mock

import pytest

from local_ai_installer import workflow
from local_ai_installer.downloader.catalog import load_catalog, load_queue, write_queue
from local_ai_installer.downloader.models import DownloadQueue, Selection
from local_ai_installer.installer import assignments, checks
from local_ai_installer.installer.docker import slots


def test_saved_assignment_enables_backup_without_changing_packaged_defaults(tmp_path):
    state = assignments.Deployment(target="example")
    state.assignments["llm-general"] = assignments.Assignment(
        model_id="qwen3-4b", variant_id="gguf-q4-k-m", enabled=True
    )
    assignments.save_deployment(tmp_path, state)
    assert assignments.deployment_slots(tmp_path)["llm-general"]["enabled"] is True
    assert slots()["llm-general"]["enabled"] is False
    assert assignments.load_deployment(tmp_path, "example") == state
    with pytest.raises(ValueError, match="different Docker context"):
        assignments.load_deployment(tmp_path, "another-target")


def test_unverified_model_cannot_be_activated(tmp_path):
    state = assignments.Deployment(target="example")
    state.assignments["image-generation-general"] = assignments.Assignment(
        model_id="qwen-image-2.1", variant_id="diffusers"
    )
    assignments.save_deployment(tmp_path, state)
    with pytest.raises(ValueError, match="unverified"):
        assignments.load_deployment(tmp_path)


def test_cross_category_assignment_is_rejected(tmp_path):
    state = assignments.Deployment(target="example")
    state.assignments["tts-german"] = assignments.Assignment(
        model_id="qwen3-4b", variant_id="gguf-q4-k-m"
    )
    assignments.save_deployment(tmp_path, state)
    with pytest.raises(ValueError, match="category"):
        assignments.load_deployment(tmp_path)


def test_category_selection_preserves_other_queued_categories(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "storage", lambda: tmp_path)
    monkeypatch.setattr(workflow, "table", Mock())
    write_queue(
        tmp_path / "download.yaml",
        DownloadQueue(
            schemaVersion=1,
            selected=[
                Selection(modelId="whisper-large-v3", variantId="ctranslate2"),
            ],
        ),
    )
    monkeypatch.setattr(
        workflow.questionary,
        "select",
        Mock(
            side_effect=[
                Mock(ask=Mock(return_value="tts")),
                Mock(ask=Mock(return_value="Select downloads")),
            ]
        ),
    )
    monkeypatch.setattr(
        workflow.questionary,
        "checkbox",
        Mock(
            return_value=Mock(ask=Mock(return_value=[("chatterbox-multilingual-v2", "pytorch-v2")]))
        ),
    )
    workflow.select_models()
    queue = load_queue(tmp_path / "download.yaml")
    assert [(s.model_id, s.variant_id) for s in queue.selected] == [
        ("whisper-large-v3", "ctranslate2"),
        ("chatterbox-multilingual-v2", "pytorch-v2"),
    ]


def test_cancelled_selection_keeps_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "storage", lambda: tmp_path)
    monkeypatch.setattr(workflow, "table", Mock())
    queue = DownloadQueue(
        schemaVersion=1, selected=[Selection(modelId="whisper-large-v3", variantId="ctranslate2")]
    )
    write_queue(tmp_path / "download.yaml", queue)
    monkeypatch.setattr(
        workflow.questionary, "select", Mock(return_value=Mock(ask=Mock(return_value=None)))
    )
    workflow.select_models()
    assert load_queue(tmp_path / "download.yaml") == queue


def test_offline_status_never_contacts_target(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "storage", lambda: tmp_path)
    docker = Mock(side_effect=AssertionError("unexpected target access"))
    monkeypatch.setattr(workflow, "Docker", docker)
    render = Mock()
    monkeypatch.setattr(workflow, "table", render)
    workflow.deployment_status()
    docker.assert_not_called()
    rows = render.call_args.args[2]
    assert all(row[3] == "Not checked" for row in rows)


def test_inference_receipt_invalidates_on_signature_change(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "signature", lambda *args: "original")
    monkeypatch.setattr(checks, "probe", Mock())
    docker = Mock()
    checks.verify(docker, tmp_path, "tts-german")
    assert checks.status(docker, tmp_path, "tts-german") == "Verified"
    monkeypatch.setattr(checks, "signature", lambda *args: "changed")
    assert checks.status(docker, tmp_path, "tts-german") == "Needs retest"


def test_failed_retest_does_not_keep_verified_status(tmp_path, monkeypatch):
    (tmp_path / "inference-checks.json").write_text(json.dumps({"tts-german": "original"}))
    monkeypatch.setattr(checks, "signature", lambda *args: "original")
    monkeypatch.setattr(checks, "probe", Mock(side_effect=ValueError("backend failed")))
    with pytest.raises(ValueError, match="backend failed"):
        checks.verify(Mock(), tmp_path, "tts-german")
    assert checks.status(Mock(), tmp_path, "tts-german") == "Not tested"


def test_presets_have_reviewed_variant_metadata():
    catalog = load_catalog()
    for preset in slots().values():
        assert (
            catalog.model(preset["model_id"]).variant(preset["variant_id"]).localai == "supported"
        )
    assert catalog.model("qwen-image-2.1").variant("diffusers").localai == "unverified"


def test_legacy_inventory_does_not_block_assignment_or_display(tmp_path, monkeypatch):
    from local_ai_installer.downloader.catalog import load_installed
    from local_ai_installer.downloader.presentation import show_installed

    records = [
        ("parakeet-tdt-0.6b-v3", "legacy"),
        ("qwen3-4b", "removed-variant"),
        ("qwen3-4b", "gguf-q4-k-m"),
    ]
    content = json.dumps(
        {
            "schemaVersion": 1,
            "installed": [
                {
                    "modelId": model,
                    "variantId": variant,
                    "source": "example/model",
                    "revision": "abc",
                    "path": f"models/{model}/{variant}",
                    "installedAt": "2026-01-01T00:00:00Z",
                    "totalBytes": 1024,
                    "fileCount": 1,
                    "verification": "sha256",
                }
                for model, variant in records
            ],
        }
    )
    inventory = tmp_path / "installed.yaml"
    inventory.write_text(content)
    monkeypatch.setattr(workflow, "storage", lambda: tmp_path)
    monkeypatch.setattr(workflow, "DeploymentSettings", lambda: Mock(docker_context="example"))
    prompt = Mock(
        side_effect=[
            Mock(ask=Mock(return_value="llm-general")),
            Mock(ask=Mock(return_value=None)),
        ]
    )
    monkeypatch.setattr(workflow.questionary, "select", prompt)
    workflow.assign_models()
    choices = prompt.call_args.kwargs["choices"]
    assert [choice.value.variant_id for choice in choices] == ["gguf-q4-k-m"]
    output = []
    show_installed(load_installed(inventory), load_catalog(), output.append)
    rendered = " ".join(" ".join(output).split())
    assert "parakeet-tdt-0.6b-v3" in rendered
    assert "Not in current catalog" in rendered
    assert inventory.read_text() == content
