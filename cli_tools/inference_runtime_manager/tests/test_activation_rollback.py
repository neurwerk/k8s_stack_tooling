from __future__ import annotations

import subprocess
import unittest
from typing import Any

from inference_runtime_manager.installer.upload import activate_with_rollback


class FakeDocker:
    def __init__(self, fail_selected: bool = False) -> None:
        self.fail_selected = fail_selected
        self.calls: list[tuple[str, object]] = []

    def worker(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("worker", payload.copy()))
        if payload["action"] == "active_destination":
            return {"destination": "library/lightonocr/revision/transformers/fingerprint"}
        return {"ok": True}

    def run(self, *args: str, **_kwargs: Any) -> None:
        self.calls.append(("run", args))
        if self.fail_selected and args[0] == "up" and args[-1] == "vlm-images-nanonets":
            self.fail_selected = False
            raise subprocess.CalledProcessError(1, args)


class ActivationRollbackTests(unittest.TestCase):
    def test_failed_recipe_restores_link_and_previous_service(self) -> None:
        docker = FakeDocker(fail_selected=True)
        application = {
            "action": "activate",
            "alias": "vlm-images",
            "service": "vlm-images-nanonets",
        }
        states = {"vlm-images": ("running", "healthy", "old-image")}

        with self.assertRaisesRegex(RuntimeError, "previous service restored"):
            activate_with_rollback(docker, application, states)

        restore = next(
            payload
            for action, payload in docker.calls
            if action == "worker"
            and isinstance(payload, dict)
            and payload.get("action") == "restore_active"
        )
        self.assertEqual(
            restore["destination"], "library/lightonocr/revision/transformers/fingerprint"
        )
        self.assertIn(
            (
                "run",
                (
                    "up",
                    "--pull",
                    "never",
                    "-d",
                    "--no-deps",
                    "--wait",
                    "--wait-timeout",
                    "300",
                    "vlm-images",
                ),
            ),
            docker.calls,
        )

    def test_success_does_not_restore_previous_link(self) -> None:
        docker = FakeDocker()
        activate_with_rollback(
            docker,
            {"action": "activate", "alias": "vlm-images", "service": "vlm-images-olmocr"},
            {"vlm-images": ("running", "healthy", "old-image")},
        )

        self.assertFalse(
            any(
                action == "worker"
                and isinstance(payload, dict)
                and payload.get("action") == "restore_active"
                for action, payload in docker.calls
            )
        )


if __name__ == "__main__":
    unittest.main()
