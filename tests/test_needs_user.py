"""Dúvidas param e esperam o usuário; LLM só depois da resposta."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.browser import ProductResult
from orchestrator.main import (
    _apply_answers_file,
    _format_duvida_block,
    _index_from_answer,
    _is_skip_answer,
    _prompt_duvidas,
)
from orchestrator.report import render_duvidas
from orchestrator.selector import Ambiguity, Decision, SelectorConfig, select


def _pr(i, name, brand="", price=10.0, cats=None):
    return ProductResult.from_dict(
        {
            "index": i,
            "name": name,
            "price_num": price,
            "price_per_base_unit": price,
            "price_base_dim": "un",
            "sale_unit": "UN",
            "brand_name": brand,
            "categories": cats or [],
        }
    )


CFG = SelectorConfig()


class NeedsUserTest(unittest.TestCase):
    def test_missing_brand_is_ambiguity_not_cheapest(self):
        item = {
            "raw": "Água",
            "item_text": "Água",
            "generic": "água mineral com gás",
            "preferred_brand": "Bioleve",
            "rotation": False,
            "qty": 12,
            "unit": "un",
            "lactose_free": False,
        }
        cands = [
            _pr(0, "Água Mineral Sem Gás Pureza Vital 1.5LT", "Pureza"),
            _pr(1, "Água Mineral com Gás Minalba 1.5LT", "Minalba"),
        ]
        out = select(item, cands, CFG)
        self.assertIsInstance(out, Ambiguity)
        self.assertEqual(out.kind, "missing_brand")

    def test_preferred_brand_still_decides(self):
        item = {
            "raw": "Água",
            "item_text": "Água",
            "generic": "água mineral com gás",
            "preferred_brand": "Bioleve",
            "rotation": False,
            "qty": 12,
            "unit": "un",
            "lactose_free": False,
        }
        cands = [
            _pr(0, "Água Mineral com Gás Bioleve 1.5LT", "Bioleve"),
            _pr(1, "Água Mineral com Gás Minalba 1.5LT", "Minalba"),
        ]
        out = select(item, cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertIn("Bioleve", out.name)

    def test_index_answer_is_not_search_text(self):
        self.assertEqual(_index_from_answer("0"), 0)
        self.assertEqual(_index_from_answer(" 2 "), 2)
        self.assertIsNone(_index_from_answer("granel 200g"))
        self.assertIsNone(_index_from_answer("pular"))

    def test_skip_answers(self):
        self.assertTrue(_is_skip_answer("pular"))
        self.assertTrue(_is_skip_answer("não quero"))
        self.assertFalse(_is_skip_answer("quero a granel 200g"))

    def test_answers_file_matches_raw(self):
        planned = [
            {"raw": "Chimichurri Andorinha granel", "status": "needs_user"},
            {"raw": "Água (12)", "status": "needs_user"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.yaml"
            p.write_text(
                "respostas:\n  - item: Chimichurri\n    texto: granel 200g\n",
                encoding="utf-8",
            )
            n = _apply_answers_file(planned, p)
        self.assertEqual(n, 1)
        self.assertEqual(planned[0]["user_answer"], "granel 200g")
        self.assertNotIn("user_answer", planned[1])

    def test_prompt_duvidas_saves_answers(self):
        planned = [
            {
                "raw": "Água (12)",
                "status": "needs_user",
                "search_term": "água mineral com gás Bioleve",
                "qty": 12,
                "unit": "un",
                "notes": ["marca não apareceu"],
                "candidates": [{"index": 0, "name": "Minalba 1.5L", "price_num": 3.0}],
            }
        ]
        answers = iter(["quero a Bioleve com gás 1,5L"])
        n = _prompt_duvidas(planned, input_fn=lambda _p: next(answers))
        self.assertEqual(n, 1)
        self.assertIn("Bioleve", planned[0]["user_answer"])
        block = _format_duvida_block(planned[0], 1, 1)
        self.assertIn("Minalba", block)
        self.assertIn("água mineral com gás Bioleve", block)

    def test_duvidas_lists_search_and_hits(self):
        md = render_duvidas(
            [
                {
                    "raw": "Chimichurri",
                    "status": "needs_user",
                    "search_term": "chimichurri granel",
                    "qty": 0.2,
                    "unit": "kg",
                    "notes": ["marca não apareceu"],
                    "candidates": [
                        {"index": 0, "name": "Tempero Chimichurri Granel Kg", "price_num": 86.9}
                    ],
                }
            ]
        )
        self.assertIn("chimichurri granel", md)
        self.assertIn("Tempero Chimichurri Granel Kg", md)
        self.assertIn("resolve", md)


if __name__ == "__main__":
    unittest.main()
