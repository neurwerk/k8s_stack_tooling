"""Exercise the real wire boundary with deterministic models, without weights or CUDA."""

import json
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import protocol


def request_body(shape=(1, 3, 448, 448), dtype="FP32"):
    image = np.zeros(shape, dtype=np.float32)
    raw = image.tobytes()
    header = json.dumps(
        {
            "inputs": [
                {
                    "name": "images",
                    "datatype": dtype,
                    "shape": list(shape),
                    "parameters": {"binary_data_size": len(raw)},
                }
            ]
        }
    ).encode()
    return header + raw, len(header)


def table_output():
    return {
        "tag_seq": np.array([2, 5, 9, 3], dtype=np.int64),
        "classes": np.array([[0.0, 1.0, 0.0]], dtype=np.float32),
        "coords": np.array([[0.5, 0.5, 0.8, 0.8]], dtype=np.float32),
    }


class ContractTests(TestCase):
    def setUp(self):
        self.models = Mock()
        self.models.infer.return_value = table_output()
        self.server = app.Server(("127.0.0.1", 0), app.Scheduler(self.models, 1), "synthetic-key")
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v2/models/tableformer/infer"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def send(self, body, length, key="synthetic-key", **headers):
        return urllib.request.urlopen(
            urllib.request.Request(
                self.url,
                data=body,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Inference-Header-Content-Length": str(length),
                    **headers,
                },
            ),
            timeout=10,
        )

    def test_auth_and_malformed_inputs_never_execute(self):
        body, length = request_body()
        cases = [
            (b"{}", 2, "wrong", {}),
            (body[:-1], length, "synthetic-key", {}),
            (*request_body(dtype="FP64"), "synthetic-key", {}),
            (*request_body(shape=(1, 3, 2, 2)), "synthetic-key", {}),
            (b"{}", 2, "synthetic-key", {"Content-Length": str(protocol.MAX_BODY + 1)}),
        ]
        for data, header_length, key, headers in cases:
            with self.subTest(size=len(data), header_length=header_length):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.send(data, header_length, key, **headers)
                self.assertIn(error.exception.code, (400, 401))
        self.models.infer.assert_not_called()

    def test_binary_roundtrip_and_model_failures(self):
        body, length = request_body()
        with self.send(body, length) as response:
            result = response.read()
            header = json.loads(result[: int(response.headers["Inference-Header-Content-Length"])])
            self.assertEqual(header["outputs"][0]["shape"], [4])
        bad = table_output()
        bad["coords"][0, 0] = np.nan
        for value in (bad, {"tag_seq": np.array([[2]])}):
            self.models.infer.return_value = value
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.send(body, length)
            self.assertEqual(error.exception.code, 500)
        self.models.infer.side_effect = RuntimeError("sensitive content")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.send(body, length)
        self.assertNotIn(b"sensitive content", error.exception.read())

    def test_scheduler_capacity_model_serialization_and_failure_release(self):
        entered = threading.Event()
        release = threading.Event()
        model = Mock()

        def block(name, inputs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test synchronization")
            return {}

        model.infer.side_effect = block
        scheduler = app.Scheduler(model, 2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(scheduler.infer, "layout", {})
            self.assertTrue(entered.wait(5))
            self.assertFalse(scheduler.locks["layout"].acquire(blocking=False))
            self.assertTrue(scheduler.capacity.acquire(blocking=False))
            self.assertFalse(scheduler.capacity.acquire(blocking=False))
            scheduler.capacity.release()
            entered.clear()
            second = pool.submit(scheduler.infer, "ocr", {})
            self.assertTrue(entered.wait(5))
            self.assertFalse(scheduler.capacity.acquire(blocking=False))
            release.set()
            first.result()
            second.result()
        model.infer.side_effect = RuntimeError("failure")
        with self.assertRaises(RuntimeError):
            scheduler.infer("layout", {})
        self.assertTrue(scheduler.locks["layout"].acquire(blocking=False))
        scheduler.locks["layout"].release()
        self.assertTrue(scheduler.capacity.acquire(blocking=False))
        self.assertTrue(scheduler.capacity.acquire(blocking=False))
        scheduler.capacity.release()
        scheduler.capacity.release()
