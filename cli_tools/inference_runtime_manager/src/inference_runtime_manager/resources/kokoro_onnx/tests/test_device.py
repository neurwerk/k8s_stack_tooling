"""CUDA requests must never silently become CPU-only inference."""

import os
import unittest
from unittest.mock import Mock, patch

import app


class DeviceTests(unittest.TestCase):
    def test_selected_device_must_initialize_and_disable_runtime_fallback(self):
        for device, provider in (
            ("cpu", "CPUExecutionProvider"),
            ("cuda", "CUDAExecutionProvider"),
        ):
            with self.subTest(device=device):
                session = Mock()
                session.get_providers.return_value = [provider, "CPUExecutionProvider"]
                with (
                    patch.dict(os.environ, {"KOKORO_DEVICE": device}),
                    patch.object(app.ort, "preload_dlls", create=True) as preload,
                    patch.object(app.ort, "get_available_providers", return_value=[provider]),
                    patch.object(app.ort, "InferenceSession", return_value=session) as create,
                    patch.object(app.Kokoro, "from_session") as kokoro,
                ):
                    result = app.load_tts()
                    self.assertEqual(create.call_args.kwargs["providers"], [provider])
                    session.disable_fallback.assert_called_once()
                    self.assertEqual(preload.call_count, int(device == "cuda"))
                    self.assertIs(result, kokoro.return_value)
                    with patch.object(app, "_tts", result):
                        self.assertEqual(app.health()["device"], device)

    def test_cuda_unavailable_or_failed_initialization_rejects_startup(self):
        for available in (
            ["CPUExecutionProvider"],
            ["CUDAExecutionProvider", "CPUExecutionProvider"],
        ):
            session = Mock()
            session.get_providers.return_value = ["CPUExecutionProvider"]
            with (
                self.subTest(available=available),
                patch.dict(os.environ, {"KOKORO_DEVICE": "cuda"}),
                patch.object(app.ort, "preload_dlls", create=True),
                patch.object(app.ort, "get_available_providers", return_value=available),
                patch.object(app.ort, "InferenceSession", return_value=session),
                patch.object(app.Kokoro, "from_session") as kokoro,
                self.assertRaises(RuntimeError),
            ):
                app.load_tts()
            kokoro.assert_not_called()

    def test_invalid_device_is_rejected(self):
        with patch.dict(os.environ, {"KOKORO_DEVICE": "auto"}), self.assertRaises(ValueError):
            app.load_tts()
