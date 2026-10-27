"""Selected password delivery survives initial and late-install interruptions."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from test_main import ControllingTerminal
from test_recovery import initialized_kit

from openbao_stack_setup.credentials import BootstrapPassword
from openbao_stack_setup.main import _deliver_bootstrap_passwords, _prepare_bootstrap_passwords
from openbao_stack_setup.recovery import (
    RecoveryKitError,
    load,
    update,
    with_checkpoint,
    with_password_delivery_complete,
    write_new,
)

DEFINITION = BootstrapPassword(
    "sample/admin", "Sample administrator", "addons/sample/admin", "password", "sha256:sample-v1"
)


@pytest.mark.parametrize("late", [False, True])
def test_pending_delivery_survives_interruption_and_acknowledgement(tmp_path: Path, late: bool):
    kit = initialized_kit()
    if late:
        kit = with_checkpoint(with_checkpoint(kit, "seeded"), "complete")
    path = tmp_path / "recovery.json"
    write_new(path, kit)
    client = MagicMock()
    client.read_secret.return_value = None
    with (
        patch(
            "openbao_stack_setup.main._deliver_bootstrap_passwords", side_effect=KeyboardInterrupt
        ),
        pytest.raises(KeyboardInterrupt),
    ):
        _prepare_bootstrap_passwords(client, kit, path, definitions=(DEFINITION,))
    pending = load(path)
    assert pending.pending_bootstrap_passwords
    assert pending.pending_password_bindings is not None
    assert pending.pending_password_bindings[DEFINITION.key]["path"] == DEFINITION.path
    assert not pending.bootstrap_passwords_acknowledged
    with pytest.raises(RecoveryKitError, match="not been acknowledged"):
        with_password_delivery_complete(pending)

    with patch("openbao_stack_setup.main._deliver_bootstrap_passwords") as deliver:
        acknowledged, values = _prepare_bootstrap_passwords(
            client, pending, path, definitions=(DEFINITION,)
        )
    deliver.assert_called_once_with(pending.pending_bootstrap_passwords, definitions=(DEFINITION,))
    assert values == pending.pending_bootstrap_passwords
    assert load(path) == acknowledged
    client.reset_mock()
    with patch("openbao_stack_setup.main._deliver_bootstrap_passwords") as deliver:
        resumed, retry_values = _prepare_bootstrap_passwords(
            client, load(path), path, definitions=(DEFINITION,)
        )
    assert resumed == acknowledged
    assert retry_values == values
    deliver.assert_not_called()
    client.read_secret.assert_not_called()
    client.write_secret.assert_not_called()
    update(path, with_password_delivery_complete(resumed))
    assert load(path).pending_bootstrap_passwords is None
    assert load(path).pending_password_bindings is None
    assert load(path).checkpoint == kit.checkpoint


@pytest.mark.parametrize("acknowledged", [False, True])
@pytest.mark.parametrize("changed", ["label", "path", "field", "definition", "deselected"])
def test_pending_definition_changes_fail_before_display_or_secret_access(
    tmp_path: Path, changed: str, acknowledged: bool
):
    path = tmp_path / "recovery.json"
    kit = initialized_kit()
    write_new(path, kit)
    client = MagicMock()
    client.read_secret.return_value = None
    with patch("openbao_stack_setup.main._deliver_bootstrap_passwords"):
        staged, _ = _prepare_bootstrap_passwords(client, kit, path, definitions=(DEFINITION,))
    staged = replace(staged, bootstrap_passwords_acknowledged=acknowledged)
    update(path, staged)
    definitions = () if changed == "deselected" else (replace(DEFINITION, **{changed: "changed"}),)
    client.reset_mock()
    with (
        patch("openbao_stack_setup.main._deliver_bootstrap_passwords") as deliver,
        pytest.raises(RecoveryKitError, match="definitions changed"),
    ):
        _prepare_bootstrap_passwords(client, load(path), path, definitions=definitions)
    deliver.assert_not_called()
    assert client.mock_calls == []
    assert load(path) == staged


def test_selected_password_uses_protected_terminal_only(monkeypatch, capsys):
    terminal = ControllingTerminal("I HAVE SAVED THESE PASSWORDS\n")
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: terminal)
    _deliver_bootstrap_passwords({DEFINITION.key: "synthetic-password"}, definitions=(DEFINITION,))
    assert "Sample administrator: synthetic-password" in terminal.getvalue()
    assert capsys.readouterr() == ("", "")
