"""Accept named KServe inputs independently of their wire order."""

import json
import struct
import sys
from pathlib import Path
from unittest import TestCase

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import protocol


class OcrInputOrderTests(TestCase):
    def setUp(self):
        self.image = np.arange(18, dtype=np.uint8).reshape(1, 2, 3, 3)
        self.inputs = {
            "image": ("UINT8", list(self.image.shape), self.image.tobytes()),
            "lang_type": ("BYTES", [1, 1], struct.pack("<I", 2) + b"ch"),
        }

    def decode(self, names):
        tensors, chunks = [], []
        for name, source in names:
            dtype, shape, raw = self.inputs[source]
            tensors.append(
                dict(
                    name=name,
                    datatype=dtype,
                    shape=shape,
                    parameters={"binary_data_size": len(raw)},
                )
            )
            chunks.append(raw)
        header = json.dumps({"inputs": tensors}).encode()
        return protocol.decode(header + b"".join(chunks), len(header), "ocr")

    def test_both_wire_orders(self):
        for order in (("image", "lang_type"), ("lang_type", "image")):
            with self.subTest(order=order):
                result = self.decode([(name, name) for name in order])
                np.testing.assert_array_equal(result["image"], self.image)
                self.assertEqual(result["image"].dtype, np.dtype("uint8"))
                np.testing.assert_array_equal(result["lang_type"], [["ch"]])
                self.assertEqual(result["lang_type"].dtype, np.dtype("object"))

    def test_invalid_names_and_missing_input(self):
        for inputs in (
            [("image", "image"), ("image", "image")],
            [("unknown", "image"), ("lang_type", "lang_type")],
            [("image", "image")],
        ):
            with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                self.decode(inputs)
