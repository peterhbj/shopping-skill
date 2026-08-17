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
        self.assertEqual(expected_unit("ovos", "carnes"), "un")
        self.assertEqual(expected_unit("alface", "hortifruti"), "un")

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

    def test_leite_em_po_not_longa_vida(self):
        cands = [
            _hit("Leite Longa Vida Semi Desnatado Jussara Zero Lactose Garrafa 1LT", ["Laticínios e Frios"], idx=0),
            _hit("Leite Condensado Semi Desnatado Moça Cx 395g", ["Mercearia"], idx=1),
            _hit("Leite em Pó Soja Soy + Original Sem Lactose Pote 300G", ["Mercearia"], idx=2),
            _hit("Leite em Pó Ninho Integral 380g", ["Mercearia"], idx=3),
        ]
        out = select(_item("Leite em pó", department="mercearia"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertIn("Pó", out.name)
        self.assertNotIn("Soja", out.name)

    def test_pipoca_prefers_microondas(self):
        cands = [
            _hit("Milho Pipoca Yoki Premium 400g", ["Mercearia", "Mercearia > Grãos, Arroz e Feijões > Pipoca"], idx=0),
            _hit("Pipoca de Microondas Yoki Pop Corn Natural com Sal 100G", ["Mercearia"], idx=1),
        ]
        out = select(_item("Pipoca", department="mercearia", brand="Yoki"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertIn("Microondas", out.name)

    def test_bife_not_moida(self):
        cands = [
            _hit("Patinho Prata Moída Kg", ["Carnes e aves"], "KG", "VARIABLE", idx=0),
            _hit("Patinho Prata Fatiado Kg", ["Carnes e aves"], "KG", "VARIABLE", idx=1),
            _hit("Patinho Prata Pedaço Kg", ["Carnes e aves"], "KG", "VARIABLE", idx=2),
        ]
        out = select(_item("Bife", generic="patinho", department="carnes", unit="kg"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertIn("Pedaço", out.name)
        self.assertNotIn("Fatiado", out.name)

    def test_ovo_not_codorna(self):
        cands = [
            _hit("Ovos De Codorna Shinoda C/30", ["Mercearia"], idx=0),
            _hit("Ovos Brancos Grandes Cartela C/20", ["Mercearia"], idx=1),
        ]
        out = select(_item("Ovo", generic="ovos", department="carnes", unit="un"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertNotIn("Codorna", out.name)

    def test_luva_prefers_tamanho_p(self):
        cands = [
            _hit("Luva Latex Forrada Laranja M Sanro Plus Embalagem 1 Par", ["Bazar e utilidades"], idx=0),
            _hit("Luva Latex Danny Amarela Tamanho P Embalagem 1 Par", ["Bazar e utilidades"], idx=1),
        ]
        out = select(_item("Luva P", generic="luva de borracha p", department="higiene"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertRegex(out.name, r"\bP\b")

    def test_chimichurri_not_sal_de_churrasco(self):
        cands = [
            _hit("Sal Para Churrasco Br Spices com Chimichurri Embalagem 350g", ["Mercearia"], idx=0),
            _hit("Tempero Chimichurri S/Pimenta Granel Kg", ["Empório"], "KG", "VARIABLE", idx=1),
        ]
        out = select(_item("Chimichurri", department="emporio"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertIn("Tempero Chimichurri", out.name)

    def test_acucar_not_fit(self):
        cands = [
            _hit("Açúcar União Fit Pacote 500G", ["Mercearia"], brand="União", idx=0),
            _hit("Açúcar Cristal União Cristalçúcar Pacote 1KG", ["Mercearia"], brand="União", idx=1),
        ]
        out = select(_item("Açúcar", department="mercearia", brand="União"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertNotIn("Fit", out.name)

    def test_refri_matches_refrigerante(self):
        cands = [
            _hit("Refrigerante de Limão Bioleve Zero 1.5LT", ["Bebidas não Alcoólicas", "Bebidas não Alcoólicas > Refrigerantes"], brand="Bioleve", idx=0),
        ]
        out = select(_item("Refri", department="mercearia", brand="Bioleve"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertIn("Refrigerante", out.name)

    def test_papel_aluminio_matches_rolo(self):
        cands = [
            _hit("Rolo De Alumínio Kiko 30x4m", ["Bazar e Utilidades", "Bazar e Utilidades > Descartáveis > Papel Alumínio"], idx=0),
        ]
        out = select(_item("Papel alumínio", department="higiene"), cands, CFG)
        self.assertIsInstance(out, Decision)
        self.assertIn("Alumínio", out.name)

    def test_empty_after_gates_is_not_found(self):
        cands = [
            _hit("Macarrão Parafuso Adria com Ovos Pacote 500G", ["Mercearia", "Mercearia > Massas"], idx=0),
        ]
        out = select(_item("Maçã", department="hortifruti"), cands, CFG)
        self.assertIsInstance(out, NoResult)


if __name__ == "__main__":
    unittest.main()
