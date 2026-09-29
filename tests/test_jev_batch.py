import io
import json
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from orchestrator.decision.jev import (
    JevAdvisor,
    JevError,
    compare_to_user_choice,
    is_confident_suggestion,
)


def entry(key, options):
    return {
        "id": key,
        "state": {"item": key},
        "question": "Escolha a opção equivalente; senão ask_user.",
        "options": options,
    }


class JevBatchTest(unittest.TestCase):
    def test_multiple_questions_use_one_request(self):
        captured = {}

        def fake_open(request, timeout):
            captured["payload"] = json.loads(request.data)
            return io.BytesIO(json.dumps({
                "model": "jev-latest",
                "answers": {
                    "item_a": {"choice": "option_0", "confidence": 0.93,
                               "probabilities": {"option_0": 0.93, "ask_user": 0.07}},
                    "item_b": {"choice": "ask_user", "confidence": 0.7,
                               "probabilities": {"option_0": 0.3, "ask_user": 0.7}},
                },
            }).encode())

        options = [{"id": "option_0", "description": "Known choice", "value": {"id": "x"}}]
        with patch("urllib.request.urlopen", side_effect=fake_open) as call:
            results = JevAdvisor("test-key").choose_many([
                entry("item_a", options), entry("item_b", options),
            ])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(set(captured["payload"]["questions"]), {"item_a", "item_b"})
        self.assertEqual(results["item_a"]["candidate"], {"id": "x"})
        self.assertIsNone(results["item_b"]["candidate"])

    def test_unknown_choice_is_rejected(self):
        response = io.BytesIO(json.dumps({
            "answers": {"item_a": {"choice": "not_sent", "probabilities": {}}}
        }).encode())
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(JevError):
                JevAdvisor("test-key").choose_many([
                    entry("item_a", [{"id": "option_0", "description": "Known", "value": 1}])
                ])

    def test_http_error_reports_detail_without_key(self):
        error = HTTPError(
            "https://api.typesafe.ai/v1/systemone", 400, "Bad Request", {},
            io.BytesIO(b'{"detail":"Invalid request for test-key"}'),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(JevError, r"HTTP 400: Invalid request for \[redacted\]"):
                JevAdvisor("test-key").choose_many([
                    entry("item_a", [{"id": "option_0", "description": "Known", "value": 1}])
                ])

    def test_auto_gate_requires_probability_and_margin(self):
        strong = {"choice": "option_0", "candidate": {"index": 7}, "confidence": 0.94,
                  "probabilities": {"option_0": 0.94, "ask_user": 0.04, "option_1": 0.02}}
        close = {**strong, "probabilities": {"option_0": 0.51, "option_1": 0.49}}
        unclear = {**strong, "choice": "ask_user", "candidate": None}
        self.assertTrue(is_confident_suggestion(strong))
        self.assertFalse(is_confident_suggestion(close, min_probability=0.5, min_margin=0.2))
        self.assertFalse(is_confident_suggestion(unclear))

    def test_user_choice_is_recorded_as_evaluation_label(self):
        suggestion = {"choice": "option_0", "candidate": {"index": 7},
                      "probabilities": {"option_0": 0.82}, "model": "jev-latest"}
        self.assertTrue(compare_to_user_choice(suggestion, 7)["match"])
        self.assertFalse(compare_to_user_choice(suggestion, 8)["match"])


if __name__ == "__main__":
    unittest.main()
