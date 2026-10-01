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


    def test_conference_turns_wrong_auto_choice_into_doubt(self):
        run = {"items": [
            {"raw": "água gás", "status": "decided", "rule": "rotation+cheapest-per-base",
             "chosen_index": 2, "chosen_name": "Água Sem Gás",
             "candidates": [{"index": 1, "name": "Água com Gás"}, {"index": 2, "name": "Água Sem Gás"}]},
            {"raw": "Arroz", "status": "decided", "rule": "preferred-brand", "chosen_index": 0,
             "candidates": [{"index": 0, "name": "Camil"}]},
        ]}
        self.assertEqual([it["raw"] for it in luna.auto_decided_items(run)], ["água gás"])
        got = luna.apply_decisions(run, {"conferencia": [
            {"id": 0, "ok": False, "index": 1, "confianca": 0.95, "motivo": "pedido é com gás"},
        ]})
        self.assertEqual(got, {"água gás": "1"})
        item = run["items"][0]
        self.assertEqual(item["status"], "needs_user")
        self.assertIsNone(item["chosen_name"])
        self.assertIn("escolha automática era: Água Sem Gás", item["notes"])

    def test_conference_ok_keeps_choice(self):
        run = {"items": [{"raw": "Sal", "status": "decided", "rule": "rotation+cheapest-per-base",
                          "chosen_index": 0, "chosen_name": "Sal Cisne",
                          "candidates": [{"index": 0, "name": "Sal Cisne"}]}]}
        luna.apply_decisions(run, {"conferencia": [{"id": 0, "ok": True}]})
        self.assertEqual(run["items"][0]["status"], "decided")

    def test_missing_item_gets_search_suggestion(self):
        run = {"items": [{"raw": "Nescan Ball", "status": "not_found", "search_term": "Nescan Ball"}]}
        luna.apply_decisions(run, {"buscas": [{"id": 0, "termo": "Nescau Ball", "motivo": "erro de digitação"}]})
        self.assertEqual(run["items"][0]["luna"]["termo"], "Nescau Ball")


if __name__ == "__main__":
    unittest.main()
