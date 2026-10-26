"""Offline adapter contracts; fake synthesis only, no models or network."""

import io
import threading
import unittest
import wave
from unittest.mock import Mock, patch

import anyio
import app
import numpy as np
from pydantic import ValidationError


class AudioTests(unittest.TestCase):
    def test_default_wav_and_pcm_bytes(self):
        request = app.SpeechRequest(model="tts-german", input="Hallo.")
        self.assertEqual(request.response_format, "wav")
        self.assertEqual((request.voice, request.language, request.speed), ("default", "de", 1.125))
        samples = np.array([-2, -1, 0, 0.5, 1, 2], dtype=np.float32)
        pcm = app.pcm_bytes(samples)
        self.assertEqual(pcm, b"\x01\x80\x01\x80\0\0\xff\x3f\xff\x7f\xff\x7f")
        with wave.open(io.BytesIO(app.wav_bytes(samples))) as wav:
            self.assertEqual(
                (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (24000, 1, 2)
            )
            self.assertEqual(wav.readframes(6), pcm)
        with self.assertRaises(ValidationError):
            app.SpeechRequest(model="tts-german", input="Hi", response_format="mp3")

    def test_first_sentence_is_yielded_before_next_synthesis(self):
        tts = Mock()
        tts.create.return_value = (np.array([0.5]), 24000)

        async def check():
            stream = app.stream_pcm(["Eins.", "Zwei."], 1.125)
            self.assertEqual(await anext(stream), b"\xff\x3f")
            self.assertEqual(tts.create.call_count, 1)
            self.assertEqual(await anext(stream), b"\0" * 12000)
            self.assertEqual(tts.create.call_count, 1)
            self.assertEqual(await anext(stream), b"\xff\x3f")
            self.assertEqual(tts.create.call_count, 2)
            await stream.aclose()

        with patch.object(app, "_tts", tts), patch.object(app, "_request_lock", anyio.Semaphore(1)):
            anyio.run(check)

    def test_disconnect_keeps_exclusivity_until_native_call_finishes(self):
        entered, release, second = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def create(**kwargs):
            calls.append(kwargs["text"])
            if kwargs["text"] == "First.":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test worker was not released")
            else:
                second.set()
            return np.array([0.5]), 24000

        async def check():
            scope = anyio.CancelScope()

            async def cancelled_request():
                with scope:
                    async for _ in app.stream_pcm(["First.", "Never."], 1):
                        self.fail("Cancelled audio must be discarded")

            async def next_request():
                async for _ in app.stream_pcm(["Second."], 1):
                    pass

            try:
                with anyio.fail_after(5):
                    async with anyio.create_task_group() as group:
                        group.start_soon(cancelled_request)
                        while not entered.is_set():
                            await anyio.sleep(0.001)
                        scope.cancel()
                        group.start_soon(next_request)
                        await anyio.sleep(0.05)
                        self.assertFalse(second.is_set())
                        self.assertEqual(app._request_lock.value, 0)
                        release.set()
            finally:
                release.set()
            self.assertEqual(calls, ["First.", "Second."])

        with (
            patch.object(app, "_tts", Mock(create=create)),
            patch.object(app, "_request_lock", anyio.Semaphore(1)),
        ):
            anyio.run(check)

    def test_wav_still_synthesizes_all_sentences_and_pause(self):
        tts = Mock()
        tts.create.return_value = (np.ones(2, dtype=np.float32), 24000)
        with patch.object(app, "_tts", tts), patch.object(app, "_normalize", lambda text: text):
            data = app.synthesize("Eins. Zwei.", 1.125)
        with wave.open(io.BytesIO(data)) as wav:
            self.assertEqual(wav.getnframes(), 6004)
        self.assertEqual(tts.create.call_count, 2)

    def test_disconnect_while_sending_closes_generator_and_unlocks(self):
        async def check():
            sent = anyio.Event()

            async def send(message):
                if message["type"] == "http.response.body":
                    sent.set()
                    await anyio.sleep_forever()

            async def receive():
                await sent.wait()
                return {"type": "http.disconnect"}

            response = app.PCMResponse(["First.", "Never."], 1)
            with anyio.fail_after(5):
                await response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
            self.assertEqual(app._request_lock.value, 1)
            self.assertEqual(tts.create.call_count, 1)

        tts = Mock()
        tts.create.return_value = (np.array([0.5]), 24000)
        with patch.object(app, "_tts", tts), patch.object(app, "_request_lock", anyio.Semaphore(1)):
            anyio.run(check)


if __name__ == "__main__":
    unittest.main()
