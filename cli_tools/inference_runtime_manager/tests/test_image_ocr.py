"""Exercise the local-image request and olmOCR prompt adapter boundaries."""

import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import cast
from unittest import TestCase
from unittest.mock import patch

from inference_runtime_manager.installer import api_tests
from inference_runtime_manager.installer.api_tests import (
    chat_completion_text,
    image_ocr_payload,
    read_image,
    read_ocr_prompt,
    sample_image,
)
from inference_runtime_manager.installer.docker import Docker
from inference_runtime_manager.resources.ocr_proxy.app import (
    OLMOCR_PROMPT,
    RequestError,
    rewrite_request,
    rewrite_response,
)


def completion(content: str, finish_reason: str = "stop") -> bytes:
    return json.dumps(
        {
            "model": "backend",
            "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
        }
    ).encode()


class ImageOCRTests(TestCase):
    def test_default_prompt_preserves_front_matter_validation(self) -> None:
        payload = image_ocr_payload("vlm-images", sample_image())
        rewritten, custom_prompt = rewrite_request(payload, "olmocr")

        self.assertFalse(custom_prompt)
        self.assertEqual(rewritten["messages"][0]["content"][0]["text"], OLMOCR_PROMPT)
        front_matter = (
            "---\nprimary_language: en\nis_rotation_valid: true\nrotation_correction: 0\n"
            "is_table: false\nis_diagram: false\n---\nTEST 123"
        )
        output = json.loads(rewrite_response(completion(front_matter), "olmocr"))
        self.assertEqual(chat_completion_text(output), "TEST 123")
        self.assertEqual(output["model"], "vlm-images")

    def test_custom_prompt_returns_plain_text_without_front_matter(self) -> None:
        payload = image_ocr_payload("vlm-images", sample_image(), prompt="Read the street sign.")
        rewritten, custom_prompt = rewrite_request(payload, "olmocr")

        self.assertTrue(custom_prompt)
        self.assertEqual(rewritten["messages"][0]["content"][0]["text"], "Read the street sign.")
        self.assertEqual(rewritten["max_tokens"], 2048)
        output = json.loads(
            rewrite_response(completion("Main Street"), "olmocr", custom_prompt=custom_prompt)
        )
        self.assertEqual(chat_completion_text(output), "Main Street")

    def test_other_ocr_profiles_reject_custom_prompts(self) -> None:
        payload = image_ocr_payload("vlm-images", sample_image(), prompt="Read this.")
        with self.assertRaisesRegex(RequestError, "only for olmOCR"):
            rewrite_request(payload, "nanonets")

    def test_custom_prompt_rejects_empty_or_oversized_text(self) -> None:
        for prompt in ("   ", "x" * 4097):
            with self.subTest(prompt_length=len(prompt)):
                payload = image_ocr_payload("vlm-images", sample_image(), prompt=prompt)
                with self.assertRaisesRegex(RequestError, "Prompt must contain"):
                    rewrite_request(payload, "olmocr")

    def test_multiple_images_and_remote_urls_stay_rejected(self) -> None:
        payload = image_ocr_payload("vlm-images", sample_image())
        payload["messages"][0]["content"].append(payload["messages"][0]["content"][0])
        with self.assertRaises(RequestError):
            rewrite_request(payload, "olmocr")

        payload = image_ocr_payload("vlm-images", sample_image())
        payload["messages"][0]["content"][0]["image_url"]["url"] = "https://example.com/image.png"
        with self.assertRaisesRegex(RequestError, "base64 image data URL"):
            rewrite_request(payload, "olmocr")

    def test_incomplete_custom_output_remains_incomplete(self) -> None:
        raw = completion("partial", finish_reason="length")
        output = json.loads(rewrite_response(raw, "olmocr", custom_prompt=True))
        self.assertEqual(output["choices"][0]["finish_reason"], "length")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            chat_completion_text(output)

    def test_custom_image_and_prompt_file(self) -> None:
        with TemporaryDirectory() as directory:
            image_path = Path(directory) / "another-page.png"
            image_path.write_bytes(sample_image())
            prompt_path = Path(directory) / "prompt.txt"
            prompt_path.write_text(
                "Read everything on this sign.\nKeep line breaks.\n", encoding="utf-8"
            )

            image = read_image(str(image_path))
            prompt = read_ocr_prompt(str(prompt_path))
            payload = image_ocr_payload("vlm-images", image, prompt=prompt)
            url = payload["messages"][0]["content"][0]["image_url"]["url"]
            self.assertEqual(base64.b64decode(url.removeprefix("data:image/png;base64,")), image)
            rewritten, custom_prompt = rewrite_request(payload, "olmocr")
            self.assertTrue(custom_prompt)
            self.assertEqual(
                rewritten["messages"][0]["content"][0]["text"],
                "Read everything on this sign.\nKeep line breaks.",
            )

    def test_custom_image_check_prints_model_output(self) -> None:
        docker = cast(Docker, SimpleNamespace(settings=SimpleNamespace(docker_context="ai-server")))
        preset = {"runtime": "llama.cpp-ocr-proxy", "health_path": "/health"}
        with (
            patch.object(api_tests, "Settings"),
            patch.object(api_tests, "management_state", return_value=Path("state")),
            patch.object(api_tests, "assigned_recipe", return_value=preset),
            patch.object(api_tests, "describe_assignment"),
            patch.object(
                api_tests, "request", side_effect=[b"{}", completion("Main Street")]
            ) as send,
            patch("builtins.print") as display,
        ):
            api_tests.test_service(
                docker, "vlm-images", None, image=sample_image(), prompt="Read the street sign."
            )

        payload = send.call_args_list[1].kwargs["payload"]
        self.assertEqual(payload["messages"][0]["content"][1]["text"], "Read the street sign.")
        display.assert_any_call("vlm-images output:\nMain Street")
