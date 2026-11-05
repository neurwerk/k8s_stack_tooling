"""Protect wire offsets, overlap ordering and resumable session identity."""

import asyncio
import os
import struct
import time
import unittest
from unittest.mock import patch

import numpy as np
from fastapi import WebSocketDisconnect

os.environ.setdefault("API_KEY", "test-only-key")
import app  # noqa: E402


class Processor:
    num_samples_first_audio_chunk = 4
    num_samples_per_audio_chunk = 4
    num_mel_frames_per_step = 1
    streaming_latency_ms = 320

    @staticmethod
    def audio_chunk_start(step):
        return step * 4


class Socket:
    def __init__(self, packets):
        self.headers = {"authorization": "Bearer test-only-key"}
        self.packets = iter(packets)
        self.output = []

    async def accept(self):
        return None

    async def receive_json(self):
        return next(self.packets)

    async def receive(self):
        return next(self.packets, {"type": "websocket.disconnect"})

    async def send_json(self, value):
        self.output.append(value)

    async def close(self, **_):
        return None


def audio(offset, samples=4):
    return {"type": "websocket.receive", "bytes": struct.pack("<Q", offset) + b"\0\0" * samples}


def control(value):
    return {"type": "websocket.receive", "text": value}


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        app.sessions.clear()
        self.processor = patch.object(app, "processor", Processor())
        self.model = patch.object(app, "inference", side_effect=self.infer)
        self.processor.start()
        self.model.start()

    def tearDown(self):
        self.model.stop()
        self.processor.stop()
        app.sessions.clear()

    @staticmethod
    def infer(session, data, **_):
        # Two simultaneous speakers on one 10 ms output frame.
        return object() if session.cache is None else session.cache, np.array([[0.9, 0.9]])

    def start(self, *packets):
        ws = Socket(
            [
                {
                    "type": "start",
                    "version": 1,
                    "encoding": "pcm_s16le",
                    "sample_rate_hz": 16000,
                    "channels": 1,
                },
                *packets,
            ]
        )
        asyncio.run(app.stream(ws))
        return ws

    def test_overlap_watermark_offsets_and_flush_cache(self):
        ws = self.start(
            audio(0),
            audio(0),
            control('{"type":"flush","through_sample":4,"request_id":"u1"}'),
            control('{"type":"flush","through_sample":4,"request_id":"u1"}'),
            audio(4),
        )
        events = ws.output
        self.assertEqual(
            [e["speaker_id"] for e in events if e["type"] == "activity"],
            ["speaker_0", "speaker_1", "speaker_0", "speaker_1"],
        )
        self.assertTrue(
            all(
                e["start_sample"] == 0 and e["end_sample"] == 4
                for e in events
                if e["type"] == "activity" and e["seq"] < 3
            )
        )
        self.assertEqual(next(e for e in events if e["type"] == "error")["expected_sample"], 4)
        self.assertEqual(
            next(e for e in events if e["type"] == "flushed")["finalized_through_sample"], 4
        )
        self.assertEqual(len([e for e in events if e["type"] == "flushed"]), 2)
        self.assertEqual(
            [e["finalized_through_sample"] for e in events if e["type"] == "progress"], [4, 8]
        )
        self.assertNotIn(
            "finalized_through_sample", next(e for e in events if e["type"] == "activity")
        )
        sid = events[0]["session_id"]
        self.assertIsNotNone(app.sessions[sid].cache)

    def test_reconnect_replay_delete_and_expiry(self):
        first = self.start(audio(0))
        sid = first.output[0]["session_id"]
        resumed = Socket(
            [
                {"type": "resume", "version": 1, "session_id": sid, "after_result_seq": 0},
                audio(4),
                control('{"type":"delete"}'),
            ]
        )
        asyncio.run(app.stream(resumed))
        self.assertEqual(resumed.output[0]["next_sample"], 4)
        self.assertEqual(
            [e["seq"] for e in resumed.output if e["type"] == "activity"], [1, 2, 3, 4]
        )
        self.assertNotIn(sid, app.sessions)
        stale = Socket([{"type": "resume", "version": 1, "session_id": sid, "after_result_seq": 0}])
        asyncio.run(app.stream(stale))
        self.assertEqual(stale.output[0]["code"], "session_expired")
        another = self.start()
        sid = another.output[0]["session_id"]
        app.sessions[sid].touched = time.monotonic() - app.TTL - 1
        expired = Socket(
            [{"type": "resume", "version": 1, "session_id": sid, "after_result_seq": 0}]
        )
        asyncio.run(app.stream(expired))
        self.assertEqual(expired.output[0]["code"], "session_expired")

    def test_auth_busy_and_replay_gap(self):
        ws = Socket([{"type": "start", "version": 1}])
        ws.headers = {}
        asyncio.run(app.stream(ws))
        self.assertFalse(ws.output)
        first = self.start(audio(0), audio(4))
        sid = first.output[0]["session_id"]
        app.sessions[sid].attached = True
        busy = Socket([{"type": "resume", "version": 1, "session_id": sid, "after_result_seq": 0}])
        asyncio.run(app.stream(busy))
        self.assertEqual(busy.output[0]["code"], "session_busy")
        app.sessions[sid].attached = False
        app.sessions[sid].replay.popleft()
        gap = Socket([{"type": "resume", "version": 1, "session_id": sid, "after_result_seq": 0}])
        asyncio.run(app.stream(gap))
        self.assertEqual(gap.output[0]["code"], "replay_gap")

    def test_disconnect_mid_overlap_replays_committed_batch(self):
        class Dropped(Socket):
            async def send_json(self, value):
                if value["type"] == "activity":
                    raise WebSocketDisconnect()
                await super().send_json(value)

        first = Dropped(
            [
                {
                    "type": "start",
                    "version": 1,
                    "encoding": "pcm_s16le",
                    "sample_rate_hz": 16000,
                    "channels": 1,
                },
                audio(0),
            ]
        )
        asyncio.run(app.stream(first))
        sid = first.output[0]["session_id"]
        self.assertEqual((app.sessions[sid].next_sample, app.sessions[sid].finalized), (4, 4))
        resumed = Socket(
            [{"type": "resume", "version": 1, "session_id": sid, "after_result_seq": 0}]
        )
        asyncio.run(app.stream(resumed))
        self.assertEqual([e["seq"] for e in resumed.output if e["type"] == "activity"], [1, 2])
        self.assertEqual(resumed.output[0]["finalized_through_sample"], 4)
        self.assertEqual(resumed.output[-1]["finalized_through_sample"], 4)
