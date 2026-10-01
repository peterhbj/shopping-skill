import unittest

import tempfile
from pathlib import Path

from orchestrator.enricher import _expand_flavors, extract_qty_from_raw, parse_list_file
from orchestrator.main import _apply_mix_answer, _mix_from_answer


class FlavorListTest(unittest.TestCase):
    def test_quantity_per_flavor(self):
        self.assertEqual(
            _expand_flavors("Suquinho: 6 maçã, 6 uva"),
            [("Suquinho (6)", "maçã"), ("Suquinho (6)", "uva")],
        )

    def test_total_is_split_between_flavors(self):
        self.assertEqual(
            _expand_flavors("Tang (5): laranja, uva"),
            [("Tang (3)", "laranja"), ("Tang (2)", "uva")],
        )

    def test_flavors_without_quantities_keep_old_behavior(self):
        self.assertEqual(
            _expand_flavors("Refri: limão, mexerica"),
            [("Refri", "limão"), ("Refri", "mexerica")],
        )


class HandwrittenListTest(unittest.TestCase):
    def _parse(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "lista.md"
            p.write_text(text, encoding="utf-8")
            return [raw for raw, _, _ in parse_list_file(p)]

    def test_plain_lines_without_bullets_are_items(self):
        self.assertEqual(self._parse("Alface\nArroz\nBife\n"), ["Alface", "Arroz", "Bife"])

    def test_short_line_before_bullets_is_still_a_heading(self):
        self.assertEqual(self._parse("Hortifruti\n- Alface\n- Tomate\n"), ["Alface", "Tomate"])

    def test_quantity_before_name(self):
        self.assertEqual(extract_qty_from_raw("2 feijão normal"), ("feijão normal", 2))
        self.assertEqual(extract_qty_from_raw("3 kg tomate"), ("3 kg tomate", None))


class FlavorMixAnswerTest(unittest.TestCase):
    def _planned(self):
        return {"raw": "Tang", "status": "needs_user", "candidates": [
            {"index": 0, "name": "Tang Laranja 18g", "price_num": 1.5},
            {"index": 3, "name": "Tang Uva 18g", "price_num": 1.6},
        ]}

    def test_parses_mix_answer(self):
        self.assertEqual(_mix_from_answer("mix:0=5,3=5"), [(0, 5), (3, 5)])
        self.assertIsNone(_mix_from_answer("3"))
        self.assertIsNone(_mix_from_answer("mix:0=0"))

    def test_mix_answer_decides_item_with_all_parts(self):
        planned = self._planned()
        self.assertTrue(_apply_mix_answer(planned, [(0, 5), (3, 5)]))
        self.assertEqual(planned["status"], "decided")
        self.assertEqual(planned["packs_needed"], 10)
        self.assertEqual(planned["total_cost"], 15.5)
        self.assertEqual([p["name"] for p in planned["mix"]], ["Tang Laranja 18g", "Tang Uva 18g"])

    def test_unknown_candidate_is_rejected(self):
        planned = self._planned()
        self.assertFalse(_apply_mix_answer(planned, [(9, 2)]))
        self.assertEqual(planned["status"], "needs_user")


if __name__ == "__main__":
    unittest.main()
