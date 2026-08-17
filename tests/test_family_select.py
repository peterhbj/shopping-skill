"""Família/unidade: os erros reais da última corrida viram fixture Sense."""
from __future__ import annotations

import unittest

from orchestrator.browser import product_from_sense_hit
from orchestrator.enricher import enrich_item, expected_unit
from orchestrator.selector import Decision, NoResult, SelectorConfig, select


def _hit(name, cats, sale="UN", typ="PRODUCT", brand="", price=10.0, idx=0):
    return product_from_sense_hit(
        {
            "id": str(idx),
            "name": name,
            "categories": [f"store1327:{c}" for c in cats],
            "saleUnit": sale,
            "type": typ,
            "brandName": brand,
            "pricing": {"price": price, "promotionalPrice": price},
            "quantity": {"sellByWeightAndUnit": sale == "KG"},
        },
        idx,
    )


def _item(raw, generic=None, department=None, unit=None, brand=None, **extra):
    generic = generic or raw
    unit = unit or expected_unit(generic, department)
    d = {
        "raw": raw,
        "item_text": raw,
        "generic": generic,
        "department": department,
        "category": department,
        "unit": unit,
        "qty": 1,
        "preferred_brand": brand,
        "rotation": not bool(brand),
        "flavor": None,
        "lactose_free": False,
    }
    d.update(extra)
    return d


CFG = SelectorConfig()


class FamilySelectTest(unittest.TestCase):
    def test_maca_not_macarrao(self):
        cands = [
            _hit("Maça Fuji Kg", ["Hortifruti", "Hortifruti > Frutas"], "KG", "VARIABLE", idx=0),
            _hit("Macarrão Espaguete com Ovos N8 Adria Pacote 500G", ["Mercearia", "Mercearia > Massas"], idx=1),
            _hit("Macarrão Parafuso Adria com Ovos Pacote 500G", ["Mercearia", "Mercearia > Massas"], idx=2),
        ]
        out = select(_item("Maçã", department="hortifruti"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertIn("Maça", out.name)
        self.assertNotIn("Macarrão", out.name)

    def test_manga_not_suco(self):
        cands = [
            _hit("Manga Palmer Kg", ["Hortifruti", "Hortifruti > Frutas"], "KG", "VARIABLE", idx=0),
            _hit("Nectar Del Valle Manga 1LT", ["Bebidas não Alcoólicas", "Bebidas não Alcoólicas > Sucos"], idx=1),
            _hit("Refresco em Pó Tang Manga 18g", ["Bebidas não Alcoólicas", "Bebidas não Alcoólicas > Sucos"], idx=2),
        ]
        out = select(_item("Manga", department="hortifruti"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertIn("Manga Palmer", out.name)

    def test_melao_not_redbull(self):
        cands = [
            _hit("Melão Amarelo Und", ["Hortifruti", "Hortifruti > Frutas"], idx=0),
            _hit(
                "Energético Red Bull Edition Maracujá e Melão Lt 250ml",
                ["Bebidas não Alcoólicas", "Bebidas não Alcoólicas > Energéticos e Isotônicos"],
                idx=1,
            ),
        ]
        out = select(_item("Melão", department="hortifruti"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertIn("Melão", out.name)
        self.assertNotIn("Red Bull", out.name)

    def test_alho_not_poro(self):
        cands = [
            _hit("Alho Granel Kg", ["Hortifruti"], "KG", "VARIABLE", idx=0),
            _hit("Alho Poró Und", ["Hortifruti"], idx=1),
            _hit("Alho Picado Bidú S/Sal 200g", ["Hortifruti"], idx=2),
        ]
        out = select(_item("Alho", department="hortifruti"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertEqual(out.name, "Alho Granel Kg")

    def test_alho_frito_not_poro(self):
        cands = [
            _hit("Alho Granel Kg", ["Hortifruti"], "KG", "VARIABLE", idx=0),
            _hit("Alho Poró Und", ["Hortifruti"], idx=1),
            _hit("Alho Frito Yoki 100g", ["Empório"], idx=2),
        ]
        out = select(_item("Alho frito", department="emporio", unit="un"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertIn("Frito", out.name)

    def test_luva_not_uva(self):
        cands = [
            _hit("Uva Thompson Bandeja 500g", ["Hortifruti", "Hortifruti > Frutas"], idx=0),
            _hit("Luva Latex Danny Amarela Tamanho P Embalagem 1 Par", ["Bazar e utilidades"], idx=1),
        ]
        out = select(_item("Luva P", generic="luva de borracha p", department="higiene"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertIn("Luva", out.name)
        self.assertNotIn("Uva", out.name)

    def test_molho_not_tomate_kg(self):
        cands = [
            _hit("Tomate Debora Kg", ["Hortifruti"], "KG", "VARIABLE", idx=0),
            _hit("Molho de Tomate Fugini Tradicional Sachê 300G", ["Mercearia"], idx=1),
        ]
        out = select(_item("Molho de tomate", department="mercearia"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertIn("Molho", out.name)

    def test_danone_not_maionese(self):
        cands = [
            _hit("Maionese Hellmann's 500g", ["Mercearia"], brand="Hellmann's", idx=0),
            _hit("Iogurte Danone Natural Integral 160G", ["Laticínios e Frios", "Laticínios e Frios > Iogurte e Bebidas Lácteas"], brand="Danone", idx=1),
        ]
        out = select(_item("Danone", department="mercearia"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertIn("Danone", out.name)

    def test_substring_uva_inside_luva_unit(self):
        self.assertEqual(expected_unit("luva de borracha p", "higiene"), "un")
        self.assertEqual(expected_unit("molho de tomate", "mercearia"), "un")
        self.assertEqual(expected_unit("Maçã", "hortifruti"), "kg")

    def test_cafe_search_keeps_3_coracoes(self):
        item = enrich_item("Café", {"marcas": {"cafe": "3 Corações"}, "apelidos": {}})
        self.assertIn("3", item.search_term)
        self.assertIn("orações", item.search_term.lower())

    def test_luva_not_borracha_de_panela(self):
        cands = [
            _hit("Borracha P/Panela Branca Clock Kimarc 5011", ["Bazar e utilidades"], idx=0),
            _hit("Luva Latex Danny Amarela Tamanho P Embalagem 1 Par", ["Bazar e utilidades"], idx=1),
        ]
        out = select(_item("Luva P", generic="luva de borracha p", department="higiene"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertIn("Luva", out.name)

    def test_uva_not_passa(self):
        cands = [
            _hit("Uva Passa Preta S/Caroço Granel Kg", ["Hortifruti"], "KG", "VARIABLE", idx=0),
            _hit("Uva Thompson Bandeja 500g", ["Hortifruti", "Hortifruti > Frutas"], idx=1),
        ]
        out = select(_item("Uva", department="hortifruti"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertIn("Thompson", out.name)

    def test_empty_after_gates_is_not_found(self):
        cands = [
            _hit("Macarrão Parafuso Adria com Ovos Pacote 500G", ["Mercearia", "Mercearia > Massas"], idx=0),
        ]
        out = select(_item("Maçã", department="hortifruti"), cands, CFG)
        self.assertIsInstance(out, NoResult)


if __name__ == "__main__":
    unittest.main()
