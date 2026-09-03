from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from openrouter_catalog_sync import main
from openrouter_catalog_sync.sync import CatalogSyncError, GeneratedFiles, Policy


def test_parse_arguments_requires_one_mode(tmp_path: Path) -> None:
    common = [
        "--catalog-output",
        str(tmp_path / "catalog"),
        "--pricing-output",
        str(tmp_path / "pricing"),
        "--policy",
        str(tmp_path / "policy"),
    ]
    with pytest.raises(SystemExit, match="2"):
        main.parse_arguments(common)
    with pytest.raises(SystemExit, match="2"):
        main.parse_arguments([*common, "--write", "--check"])


def test_main_write_uses_injected_source_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    policy = Policy({}, frozenset())
    generated = GeneratedFiles(b"catalog", b"pricing")
    fetch = Mock(return_value=[])
    write = Mock()
    monkeypatch.setattr(main, "load_policy", Mock(return_value=policy))
    monkeypatch.setattr(main, "fetch_models", fetch)
    monkeypatch.setattr(main, "generate", Mock(return_value=generated))
    monkeypatch.setattr(main, "write_files", write)
    catalog = tmp_path / "catalog"
    pricing = tmp_path / "pricing"

    main.main(
        [
            "--catalog-output",
            str(catalog),
            "--pricing-output",
            str(pricing),
            "--policy",
            str(tmp_path / "policy"),
            "--source-url",
            "https://example.test/models",
            "--write",
        ]
    )

    fetch.assert_called_once_with("https://example.test/models")
    write.assert_called_once_with(generated, catalog, pricing)


def test_main_check_prints_concise_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(main, "load_policy", Mock(side_effect=CatalogSyncError("bad policy")))

    with pytest.raises(SystemExit, match="1"):
        main.main(
            [
                "--catalog-output",
                str(tmp_path / "catalog"),
                "--pricing-output",
                str(tmp_path / "pricing"),
                "--policy",
                str(tmp_path / "policy"),
                "--check",
            ]
        )

    assert capsys.readouterr().err == "error: bad policy\n"


def test_main_rejects_policy_output_alias_before_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    fetch = Mock()
    monkeypatch.setattr(main, "fetch_models", fetch)

    with pytest.raises(SystemExit, match="1"):
        main.main(
            [
                "--catalog-output",
                str(policy),
                "--pricing-output",
                str(tmp_path / "pricing.json"),
                "--policy",
                str(policy),
                "--write",
            ]
        )

    fetch.assert_not_called()
