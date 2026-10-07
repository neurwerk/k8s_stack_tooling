"""Reject incomplete or malformed remote token-classification replies."""

from unittest import TestCase

from inference_runtime_manager.installer.api_tests import (
    validate_gliner_entities,
    validate_ner_predictions,
)


class NERPredictionTests(TestCase):
    def test_gliner_checks_span_bounds_and_finite_confidence(self) -> None:
        entity = {"start": 0, "end": 4, "label": "person", "score": 0.9}
        response = {"model": "ner-english", "entities": [entity]}
        self.assertEqual(validate_gliner_entities(response, "Jane", "ner-english"), 1)
        for change in ({"end": 5}, {"start": True}, {"score": float("nan")}, {"label": ""}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_gliner_entities(
                    {"model": "ner-english", "entities": [{**entity, **change}]},
                    "Jane",
                    "ner-english",
                )

    def test_preserves_each_instance_token_sequence(self) -> None:
        sequences = [[0, 1], [1]]
        self.assertEqual(
            validate_ner_predictions(
                {"predictions": [[{"0": 0.8, "1": 0.2}, {"0": 0.1, "1": 0.9}], [{"1": 1}]]},
                2,
            ),
            sequences,
        )

    def test_rejects_missing_instances_and_invalid_labels(self) -> None:
        for value in (
            None,
            {},
            {"predictions": []},
            {"predictions": [[{"0": 1}]]},
            {"predictions": [[{"0": 1}], []]},
            {"predictions": [[{"0": 1}], [True]]},
            {"predictions": [[{"0": 1}], [{"0": float("nan")}]]},
            {"predictions": [[{"0": 1}], [{"0": float("inf")}]]},
            {"predictions": [[{"0": 1}], [{"0": -0.1, "1": 1.1}]]},
            {"predictions": [[{"0": 1}], [{"PERSON": 1}]]},
            {"predictions": [[{"0": 1}], [{"0": 0.1}]]},
            {"predictions": [[{"0": 1}], [{"0": True}]]},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_ner_predictions(value, 2)
