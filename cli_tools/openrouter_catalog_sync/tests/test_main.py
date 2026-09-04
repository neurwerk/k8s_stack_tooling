from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from openrouter_catalog_sync import main
from openrouter_catalog_sync.sync import CatalogSyncError, GeneratedFiles, Policy, SelectionChoice


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
    policy = Policy((), {}, {}, {}, True)
    generated = GeneratedFiles(b"policy", b"catalog", b"pricing")
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
    write.assert_called_once_with(generated, tmp_path / "policy", catalog, pricing)


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


def test_prompt_uses_fuzzy_multiselect_and_preselects_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = Mock()
    prompt.execute.return_value = ["acme/one"]
    fuzzy = Mock(return_value=prompt)
    monkeypatch.setattr(main.inquirer, "fuzzy", fuzzy)

    result = main._prompt_for_models(
        [
            SelectionChoice("acme/one", "One [acme/one]"),
            SelectionChoice("acme/two", "Two [acme/two]"),
        ],
        ("acme/one",),
    )

    assert result == ["acme/one"]
    arguments = fuzzy.call_args.kwargs
    assert arguments["multiselect"] is True
    assert arguments["mandatory"] is False
    assert arguments["max_height"] == 20
    assert "Space toggles" in arguments["instruction"]
    assert [choice.enabled for choice in arguments["choices"]] == [False, True, False]


@pytest.mark.parametrize("available", [[], [SelectionChoice("acme/one", "One [acme/one]")]])
def test_prompt_permits_explicit_empty_selection(
    monkeypatch: pytest.MonkeyPatch, available: list[SelectionChoice]
) -> None:
    def fuzzy(**arguments: object) -> Mock:
        choices = arguments["choices"]
        assert isinstance(choices, list)
        assert choices[0].value is None
        assert choices[0].enabled is True
        prompt = Mock()
        prompt.execute.return_value = [choices[0].value]
        return prompt

    monkeypatch.setattr(main.inquirer, "fuzzy", fuzzy)

    assert main._prompt_for_models(available, ()) == []


def test_prompt_prefers_real_choices_over_initial_empty_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = Mock()
    prompt.execute.return_value = [None, "acme/one"]
    monkeypatch.setattr(main.inquirer, "fuzzy", Mock(return_value=prompt))

    assert main._prompt_for_models([SelectionChoice("acme/one", "One [acme/one]")], ()) == [
        "acme/one"
    ]


def test_prompt_existing_selection_can_be_replaced_with_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = Mock()
    prompt.execute.return_value = [None, "acme/one"]
    monkeypatch.setattr(main.inquirer, "fuzzy", Mock(return_value=prompt))

    assert (
        main._prompt_for_models([SelectionChoice("acme/one", "One [acme/one]")], ("acme/one",))
        == []
    )


def test_select_cancellation_leaves_all_files_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    policy_path = tmp_path / "policy.json"
    catalog_path = tmp_path / "catalog.yaml"
    pricing_path = tmp_path / "pricing.json"
    originals = (b"old policy", b"old catalog", b"old pricing")
    for path, content in zip((policy_path, catalog_path, pricing_path), originals, strict=True):
        path.write_bytes(content)
    policy = Policy((), {}, {}, {}, True)
    monkeypatch.setattr(main, "load_policy", Mock(return_value=policy))
    monkeypatch.setattr(main, "fetch_models", Mock(return_value=[]))
    monkeypatch.setattr(main, "selection_choices", Mock(return_value=[]))
    monkeypatch.setattr(
        main,
        "_prompt_for_models",
        Mock(side_effect=CatalogSyncError("model selection was cancelled")),
    )
    write = Mock()
    monkeypatch.setattr(main, "write_files", write)

    with pytest.raises(SystemExit, match="1"):
        main.main(
            [
                "--catalog-output",
                str(catalog_path),
                "--pricing-output",
                str(pricing_path),
                "--policy",
                str(policy_path),
                "--select",
            ]
        )

    write.assert_not_called()
    assert (
        tuple(path.read_bytes() for path in (policy_path, catalog_path, pricing_path)) == originals
    )
