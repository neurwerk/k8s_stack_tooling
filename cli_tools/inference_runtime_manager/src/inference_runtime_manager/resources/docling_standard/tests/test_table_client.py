"""Keep upstream table preprocessing/matching local while replacing neural inference."""

import sys
import threading
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

import numpy as np
import torch
from docling_ibm_models.tableformer.data_management.tf_predictor import TFPredictor
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import client

CONFIG = {
    "model": {"type": "TableModel04_rs"},
    "dataset": {
        "resized_image": 448,
        "keep_AR": False,
        "up_scaling_enabled": True,
        "down_scaling_enabled": True,
        "image_normalization": {"state": True, "mean": [0.9] * 3, "std": [0.2] * 3},
    },
    "predict": {
        "max_steps": 1024,
        "beam_size": 5,
        "bbox": True,
        "pdf_cell_iou_thres": 0.05,
        "padding": False,
        "profiling": False,
    },
    "dataset_wordmap": {
        "word_map_tag": {
            "<pad>": 0,
            "<unk>": 1,
            "<start>": 2,
            "<end>": 3,
            "ecel": 4,
            "fcel": 5,
            "lcel": 6,
            "ucel": 7,
            "xcel": 8,
            "nl": 9,
            "ched": 10,
            "rhed": 11,
            "srow": 12,
        }
    },
}


class FakeNetwork:
    def eval(self):
        return self

    def predict(self, images, max_steps, beam_size):
        return (
            [2, 5, 9, 3],
            torch.tensor([[0.0, 1.0, 0.0]]),
            torch.tensor([[0.5, 0.5, 0.8, 0.8]]),
        )


class DirectPredictor(TFPredictor):
    def _load_model(self):
        self._model_type = "TableModel04_rs"
        self._remove_padding = False
        return FakeNetwork()


class TableClientTests(TestCase):
    def test_proxy_retains_real_local_matching_without_loading_weights(self):
        network = FakeNetwork()
        models = Mock()

        def infer(name, inputs):
            self.assertEqual(name, "tableformer")
            self.assertEqual(inputs["images"].shape, (1, 3, 448, 448))
            tags, classes, coords = network.predict(
                torch.from_numpy(inputs["images"].copy()), 1024, 5
            )
            return {
                "tag_seq": np.array(tags, dtype=np.int64),
                "classes": classes.numpy(),
                "coords": coords.numpy(),
            }

        models.infer.side_effect = infer
        server = app.Server(("127.0.0.1", 0), app.Scheduler(models, 1), "test")
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            options = client.RemoteTableOptions(
                url=f"http://127.0.0.1:{server.server_port}", api_key=SecretStr("test")
            )
            with patch.object(
                TFPredictor, "_load_model", side_effect=AssertionError("Local weights")
            ):
                remote = client.RemotePredictor(CONFIG, options)
            direct = DirectPredictor(CONFIG, "cpu", 4)
            page = {
                "width": 100,
                "height": 100,
                "image": np.full((100, 100, 3), 255, dtype=np.uint8),
                "tokens": [
                    {"id": 0, "text": "CHECK", "bbox": {"l": 30, "t": 30, "r": 70, "b": 50}}
                ],
            }
            expected = direct.multi_table_predict(page, [[0, 0, 100, 100]], do_matching=True)
            actual = remote.multi_table_predict(page, [[0, 0, 100, 100]], do_matching=True)
            self.assertEqual(actual, expected)
            self.assertIn("CHECK", str(actual))
            models.infer.assert_called_once()
            remote.get_model().close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
