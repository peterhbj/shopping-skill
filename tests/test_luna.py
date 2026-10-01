import unittest

from webapp import luna


def _run():
    return {"items": [
        {"raw": "Arroz", "status": "decided"},
        {"raw": "Molho (10)", "status": "needs_user",
         "candidates": [{"index": 0, "name": "Heinz"}, {"index": 1, "name": "Fugini"}]},
        {"raw": "Água (12)", "status": "needs_user",
         "candidates": [{"index": 0, "name": "Bioleve 510ml"}]},
    ]}


class LunaTest(unittest.TestCase):
    def test_only_confident_valid_choices_are_returned(self):
        run = _run()
        got = luna.apply_decisions(run, {"decisoes": [
            {"id": 0, "index": 1, "confianca": 0.9, "motivo": "mais barato"},
            {"id": 1, "index": 0, "confianca": 0.5, "motivo": "talvez"},
        ]})
        self.assertEqual(got, {"Molho (10)": "1"})
        self.assertEqual(run["items"][2]["luna"]["index"], 0)

    def test_index_outside_candidates_is_ignored(self):
        run = _run()
        got = luna.apply_decisions(run, {"decisoes": [{"id": 0, "index": 7, "confianca": 1}]})
        self.assertEqual(got, {})
        self.assertIsNone(run["items"][1]["luna"]["index"])

    def test_prompt_lists_only_pending_items(self):
        prompt = luna.build_prompt(_run(), "marcas: {}")
        self.assertIn("Molho (10)", prompt)
        self.assertNotIn('"Arroz"', prompt)


if __name__ == "__main__":
    unittest.main()
