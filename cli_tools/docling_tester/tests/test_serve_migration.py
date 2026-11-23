import json
import stat

import pytest

from docling_tester.cli import parser
from docling_tester.corpus import Case
from docling_tester.runner import Settings, expectation, rejection
from docling_tester.settings import RETIRED, load, save
from docling_tester.transport import Reply


def test_legacy_settings_preserve_credentials_and_remove_retired_fields(tmp_path, capsys):
    path = tmp_path / "settings.jsonc"
    path.write_text(
        json.dumps(
            {
                "direct_transport": "docker",
                "docker_context": "old",
                "pipeline": "vlm",
                "reference_profile": "standard-no-ocr",
                "direct_api_key": "direct-sentinel",
                "gateway_api_key": "gateway-sentinel",
                "gateway_model": "example-model",
            }
        )
    )
    values = load(path)
    save(path, values)
    assert load(path) == values
    assert not values.keys() & RETIRED
    assert values["direct_api_key"] == "direct-sentinel"
    assert values["gateway_api_key"] == "gateway-sentinel"
    assert values["gateway_model"] == "example-model"
    assert values["reference_profile"] == "auto"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "flag", ["--pipeline", "--vlm-preset", "--ocr-preset", "--docker-context", "--no-ocr"]
)
def test_retired_cli_options_rejected(flag):
    with pytest.raises(SystemExit):
        parser().parse_args(
            [
                "run",
                "direct",
                "--url",
                "https://example.invalid",
                "--output",
                "results",
                flag,
                "old",
            ]
        )


def test_budgets_and_declared_limits_are_independent(tmp_path):
    settings = Settings("direct", "https://example.invalid", tmp_path)
    assert settings.timeout == 360
    assert settings.options()["document_timeout"] == "300"
    assert settings.direct_page_limit == 250
    assert settings.gateway_page_limit == 2
    settings.gateway_page_limit = 250
    assert settings.options()["document_timeout"] == "300"


@pytest.mark.parametrize("mode", ["direct", "gateway"])
def test_rejection_requires_mode_status_and_machine_reason(tmp_path, mode):
    case = Case("example.pdf", "pdf", "pdf", (), ())
    settings = Settings(mode, "https://example.invalid", tmp_path, model="example")
    rule = {"outcome": "reject", "http_status": 413, "reason": "page_limit"}
    rules = {case.path: {mode: rule}}
    assert expectation(case, settings, rules) == rule
    for body in ({}, {"detail": "page_limit"}, {"code": "other"}):
        assert rejection(Reply(413, body, 0), rule)[0] == "FAIL"
    assert rejection(Reply(413, {"code": "page_limit"}, 0), rule)[0] == "EXPECTED_REJECTION"
    assert rejection(Reply(400, {"code": "page_limit"}, 0), rule)[0] == "FAIL"
    settings.mode = "gateway" if mode == "direct" else "direct"
    assert expectation(case, settings, rules) == {"outcome": "convert"}
