"""Keep real final samples in the feature frames sent to inference."""

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import torch
from test_protocol import Socket, app
from transformers import Nemotron3DiarizationProcessor, NemotronAsrStreamingFeatureExtractor


class FrameModel:
    """Deterministic overlapping logits with the real model's look-ahead trimming."""

    device = torch.device("cpu")
    dtype = torch.float32

    def __call__(self, input_features, speaker_cache, num_lookahead_frames=0, **_):
        self.last_features = input_features.detach().clone()
        frames = input_features.shape[1] - num_lookahead_frames * 8
        return SimpleNamespace(
            speaker_cache=object() if speaker_cache is None else speaker_cache,
            logits=torch.full((1, frames, 2), 4.0),
        )


class FinalFrameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(1)
        cls.processor = Nemotron3DiarizationProcessor(
            NemotronAsrStreamingFeatureExtractor(feature_size=128, return_attention_mask=True),
            streaming_mode="ultra_low_latency",
        )
        cls.silence_frame = cls.processor(
            np.zeros(160, dtype=np.float32),
            sampling_rate=16000,
            is_streaming=True,
            is_last_audio_chunk=True,
        )["input_features"][0, 0]

    def check_length(self, samples):
        waveform = np.zeros(samples, dtype="<i2")
        waveform[-1] = 16384
        pcm = waveform.tobytes()
        session = app.Session("synthetic", "test", time.monotonic())
        ws = Socket([])
        model = FrameModel()

        async def run():
            for start in range(0, len(pcm), app.MAX_PACKET_SAMPLES * 2):
                packet = pcm[start : start + app.MAX_PACKET_SAMPLES * 2]
                session.buffer.extend(packet)
                session.next_sample += len(packet) // 2
                await app.process(session, ws)
            await app.process(session, ws, flush=True)

        with patch.object(app, "processor", self.processor), patch.object(app, "model", model):
            asyncio.run(run())
        self.assertEqual((session.next_sample, session.finalized), (samples, samples))
        events = [event for event in ws.output if event["type"] == "activity"]
        self.assertEqual(len(events), ((samples + 159) // 160) * 2)
        self.assertEqual([event["speaker_id"] for event in events[-2:]], ["speaker_0", "speaker_1"])
        self.assertEqual([event["end_sample"] for event in events[-2:]], [samples, samples])
        self.assertTrue(
            all(0 <= event["start_sample"] < event["end_sample"] <= samples for event in events)
        )
        # A forced watermark or silent fake frame cannot satisfy this: the
        # last real impulse must reach the model's final mel feature frame.
        self.assertFalse(torch.equal(model.last_features[0, -1], self.silence_frame))

    def test_partial_first_frame_reaches_model(self):
        for samples in (159, 160, 161):
            with self.subTest(samples=samples):
                self.check_length(samples)

    def test_chunk_boundaries_and_reported_wav_length(self):
        first = self.processor.num_samples_first_audio_chunk
        later = (
            self.processor.audio_chunk_start(self.processor.num_mel_frames_per_step)
            + self.processor.num_samples_per_audio_chunk
        )
        for samples in (first - 1, first, first + 1, later - 1, later, later + 1, 200000, 200096):
            with self.subTest(samples=samples):
                self.check_length(samples)
