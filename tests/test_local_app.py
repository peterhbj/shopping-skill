import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from orchestrator.browser import product_from_sense_hit
from orchestrator.fast_plan import _history_decision, _strict_equivalent, package_size, plan_fast
from orchestrator.evaluate import replay
from orchestrator.history import PurchaseHistory
from orchestrator.main import _apply_one
from orchestrator.selector import SelectorConfig


def product(index, pid, name, price, brand):
    result = product_from_sense_hit({"id": pid, "name": name,
                                    "brandName": brand,
                                    "pricing": {"price": price},
                                    "saleUnit": "UN"}, index)
    size = package_size(name)
    if size:
        result.price_base_dim = size[0]
        result.price_per_base_unit = price / size[1]
    return result


class HistoryTest(unittest.TestCase):
    def test_only_completed_orders_are_imported_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as temp:
            history = PurchaseHistory(Path(temp) / "purchases.sqlite3")
            orders = [
                {"order_id": "a", "date": "2026-08-01", "status": "entregue",
                 "items": [{"product_id": "10", "name": "Feijão Carioca Camil 1kg", "quantity": 2}]},
                {"order_id": "b", "date": "2026-08-02", "status": "cancelado",
                 "items": [{"product_id": "11", "name": "Feijão Preto 1kg"}]},
            ]
            self.assertEqual(history.import_orders(orders, "test")["added"], 1)
            self.assertEqual(history.import_orders(orders, "test")["added"], 0)
            self.assertEqual(history.counts(), {"orders": 1, "lines": 1, "corrections": 0})
            self.assertEqual(len(history.related("feijão carioca", before="2026-07-01")), 0)
            history.close()

    def test_repeat_or_strict_15_percent_saving(self):
        item = {"raw": "Feijão carioca", "qty": 2, "unit": "un",
                "lactose_free": False, "preferred_brand": "Camil"}
        regular = product(0, "a", "Feijão Carioca Tipo 1 Camil Pacote 1kg", 10, "Camil")
        cheaper = product(1, "b", "Feijão Carioca Tipo 1 Andorinha Pacote 1kg", 8, "Andorinha")
        wrong_kind = product(2, "c", "Feijão Preto Tipo 1 Andorinha Pacote 1kg", 5, "Andorinha")
        evidence = [{"product_id": "a", "name": regular.name}]
        self.assertTrue(_strict_equivalent(regular, cheaper, item))
        self.assertFalse(_strict_equivalent(regular, wrong_kind, item))
        decision = _history_decision(item, [regular, cheaper, wrong_kind], evidence)
        self.assertEqual(decision.index, 1)
        self.assertEqual(decision.rule, "history-equivalent-15pct")
        item["raw"] = "Feijão carioca Camil"
        self.assertEqual(_history_decision(item, [regular, cheaper], evidence).index, 0)

    def test_gas_and_pack_size_are_material(self):
        item = {"raw": "Água mineral com gás", "lactose_free": False}
        regular = product(0, "a", "Água Mineral com Gás Bioleve 510ml", 3, "Bioleve")
        flat = product(1, "b", "Água Mineral sem Gás Crystal 510ml", 1, "Crystal")
        large = product(2, "c", "Água Mineral com Gás Crystal 1.5L", 1, "Crystal")
        self.assertFalse(_strict_equivalent(regular, flat, item))
        self.assertFalse(_strict_equivalent(regular, large, item))

    def test_replay_excludes_current_and_future_orders(self):
        with tempfile.TemporaryDirectory() as temp:
            history = PurchaseHistory(Path(temp) / "purchases.sqlite3")
            history.import_orders([
                {"order_id": "first", "date": "2026-01-01", "status": "entregue",
                 "items": [{"product_id": "a", "name": "Feijão Carioca Camil 1kg"}]},
                {"order_id": "target", "date": "2026-02-01", "status": "entregue",
                 "items": [{"product_id": "b", "name": "Feijão Carioca X 1kg"}]},
            ], "test")
            case = {"request": "Feijão Carioca", "ordered_at": "2026-02-01",
                    "expected_product_id": "b", "candidates": [
                        {"product_id": "a", "name": "Feijão Carioca Camil 1kg", "price_num": 10},
                        {"product_id": "b", "name": "Feijão Carioca X 1kg", "price_num": 10},
                    ]}
            report = replay([case], history)
            self.assertEqual(report["automatic"], 1)
            self.assertEqual(report["correct"], 0)
            self.assertFalse(report["goal_98pct_supported"])
            history.close()

    def test_historical_sku_is_searched_even_if_profile_names_other_brand(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            shopping_list = directory / "list.md"
            shopping_list.write_text("- [ ] Arroz\n", encoding="utf-8")
            history = PurchaseHistory(directory / "purchases.sqlite3")
            old_name = "Arroz Tipo 1 Tio Joao Pacote 5kg"
            history.import_orders([{"order_id": "prior", "date": "2026-01-01",
                                    "status": "entregue", "items": [
                                        {"product_id": "old", "name": old_name}]}], "test")
            available = product(0, "new", "Arroz Tipo 1 Camil Pacote 5kg", 27, "Camil")
            habitual = product(0, "old", old_name, 30, "Tio Joao")
            def fake_search(coroutine):
                coroutine.close()
                return {"arroz camil": [available], "arroz tipo 1 tio joao pacote 5kg": [habitual]}, {}
            with patch("orchestrator.fast_plan.asyncio.run", side_effect=fake_search):
                plan = plan_fast(shopping_list, Path(__file__).resolve().parents[1] / "preferencias.yaml",
                                 history, directory / "run.json", jev_shadow=False)
            self.assertEqual(plan["items"][0]["chosen_product_id"], "old")
            history.close()


class ApplyValidationTest(unittest.TestCase):
    def test_price_change_blocks_cart_click(self):
        class Browser:
            def search(self, query):
                pass
            def get_results(self):
                return [replace(product(0, "sku", "Feijão Carioca 1kg", 11, "Camil"), has_add=True)]
            def set_qty(self, *args, **kwargs):
                raise AssertionError("cart click must not happen")
        planned = {"raw": "Feijão", "status": "decided", "search_term": "feijão",
                   "chosen_product_id": "sku", "chosen_name": "Feijão Carioca 1kg",
                   "price_num": 10, "qty": 1, "packs_needed": 1}
        result = _apply_one(Browser(), planned, {}, SelectorConfig())
        self.assertEqual(result.status, "failed_to_add")

    def test_different_sku_blocks_cart_click(self):
        class Browser:
            def search(self, query):
                pass
            def get_results(self):
                return [replace(product(0, "other", "Feijão Carioca 1kg", 10, "Camil"), has_add=True)]
            def set_qty(self, *args, **kwargs):
                raise AssertionError("cart click must not happen")
        planned = {"raw": "Feijão", "status": "decided", "search_term": "feijão",
                   "chosen_product_id": "wanted", "chosen_name": "Feijão Carioca 1kg",
                   "price_num": 10, "qty": 1, "packs_needed": 1}
        self.assertEqual(_apply_one(Browser(), planned, {}, SelectorConfig()).status, "not_found")


if __name__ == "__main__":
    unittest.main()


def test_packs_for_qty_rounds_fractions_up():
    from orchestrator.selector import packs_for_qty
    assert packs_for_qty(1.5, "un") == 2
    assert packs_for_qty(3, "un") == 3
    assert packs_for_qty(0.4, "un") == 1
    assert packs_for_qty(2.5, "kg") == 1
    assert packs_for_qty("x") == 1
