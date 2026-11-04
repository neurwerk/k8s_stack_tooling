"""Offline adapter contracts; fake synthesis only, no models or network."""

import io
import json
import threading
import unittest
import wave
from unittest.mock import Mock, patch

import anyio
import app
import numpy as np
from pydantic import ValidationError


def fake_tts():
    tts = Mock()
    tts.tokenizer.phonemize.side_effect = lambda text, lang: "" if text == "..." else text
    tts.create.return_value = (np.array([0.5]), 24000)
    return tts


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
        tts = fake_tts()
        tts.create.return_value = (np.ones(2, dtype=np.float32), 24000)
        with patch.object(app, "_tts", tts), patch.object(app, "_normalize", lambda text: text):
            data = app.synthesize("Eins. Zwei.", 1.125)
        with wave.open(io.BytesIO(data)) as wav:
            self.assertEqual(wav.getnframes(), 6004)
        self.assertEqual(tts.create.call_count, 2)

    def test_unspeakable_fragments_are_skipped_in_wav_and_pcm(self):
        tts = fake_tts()

        async def check_pcm():
            response = await app.speech(
                app.SpeechRequest(
                    model="tts-german", input="...\nHallo.\n...\nWelt.\n...", response_format="pcm"
                )
            )
            return b"".join([chunk async for chunk in response.body_iterator])

        with (
            patch.object(app, "_tts", tts),
            patch.object(app, "_normalize", lambda text: text),
            patch.object(app, "_request_lock", anyio.Semaphore(1)),
        ):
            pcm = anyio.run(check_pcm)
            with wave.open(
                io.BytesIO(app.synthesize("...\nHallo.\n...\nWelt.\n...", 1.125))
            ) as wav:
                self.assertEqual(pcm, wav.readframes(wav.getnframes()))
        self.assertEqual(pcm, b"\xff\x3f" + bytes(12000) + b"\xff\x3f")
        self.assertEqual(
            [call.kwargs["text"] for call in tts.create.call_args_list],
            ["Hallo.", "Welt.", "Hallo.", "Welt."],
        )
        self.assertTrue(all(call.kwargs["is_phonemes"] for call in tts.create.call_args_list))

    def test_empty_speech_is_rejected_before_headers_and_next_request_works(self):
        tts = fake_tts()

        async def request(text, response_format):
            # Exercise the ASGI application, including the actual response-start
            # boundary. A generator-only test misses the former HTTP 200 bug.
            payload = json.dumps(
                {"model": "tts-german", "input": text, "response_format": response_format}
            ).encode()
            sent = []
            received = False

            async def receive():
                nonlocal received
                if not received:
                    received = True
                    return {"type": "http.request", "body": payload, "more_body": False}
                await anyio.sleep_forever()

            async def send(message):
                sent.append(message)

            await app.app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0", "spec_version": "2.3"},
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/v1/audio/speech",
                    "raw_path": b"/v1/audio/speech",
                    "query_string": b"",
                    "headers": [(b"content-type", b"application/json")],
                    "client": ("127.0.0.1", 1),
                    "server": ("127.0.0.1", 8881),
                },
                receive,
                send,
            )
            status = next(item["status"] for item in sent if item["type"] == "http.response.start")
            body = b"".join(
                item.get("body", b"") for item in sent if item["type"] == "http.response.body"
            )
            return status, body

        async def check():
            for response_format in ("pcm", "wav"):
                for text in ("...", " \n "):
                    status, body = await request(text, response_format)
                    self.assertEqual(status, 422)
                    self.assertEqual(json.loads(body)["detail"]["code"], "no_speakable_text")
                status, body = await request("Hallo.", response_format)
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertEqual(app._request_lock.value, 1)
            # An unrelated model failure is not mistaken for empty phonemes.
            tts.create.side_effect = ValueError("synthetic-private-upstream-detail")
            status, body = await request("Hallo.", "wav")
            self.assertEqual(status, 500)
            self.assertNotIn(b"synthetic-private", body)

        with (
            patch.object(app, "_tts", tts),
            patch.object(app, "_normalize", lambda text: text),
            patch.object(app, "_request_lock", anyio.Semaphore(1)),
            patch.dict(app.os.environ, {"API_KEY": ""}),
        ):
            anyio.run(check)

    def test_cancel_during_preparation_releases_native_lock(self):
        entered, release = threading.Event(), threading.Event()
        tts = fake_tts()

        def phonemize(text, lang):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test worker was not released")
            return text

        tts.tokenizer.phonemize.side_effect = phonemize

        async def check():
            scope = anyio.CancelScope()

            async def preparing():
                with scope:
                    response = await app.speech(
                        app.SpeechRequest(model="tts-german", input="Hallo.", response_format="pcm")
                    )
                    await response.stream.aclose()
                    await anyio.lowlevel.checkpoint()

            try:
                with anyio.fail_after(5):
                    async with anyio.create_task_group() as group:
                        group.start_soon(preparing)
                        while not entered.is_set():
                            await anyio.sleep(0.001)
                        scope.cancel()
                        self.assertTrue(app._lock.locked())
                        release.set()
                self.assertFalse(app._lock.locked())
                tts.create.assert_not_called()
                response = await app.speech(
                    app.SpeechRequest(model="tts-german", input="Hallo.", response_format="pcm")
                )
                self.assertEqual(
                    b"".join([chunk async for chunk in response.body_iterator]), b"\xff\x3f"
                )
            finally:
                release.set()

        with (
            patch.object(app, "_tts", tts),
            patch.object(app, "_normalize", lambda text: text),
            patch.object(app, "_request_lock", anyio.Semaphore(1)),
        ):
            anyio.run(check)

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
