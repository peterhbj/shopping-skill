import json
import tempfile
import unittest
from pathlib import Path

import yaml

from orchestrator.main import _apply_answers_file
from webapp import server


class AnswerMatchingTest(unittest.TestCase):
    def test_exact_name_wins_over_longer_names(self):
        planned = [{"raw": r, "status": "needs_user"}
                   for r in ("Leite em pó", "Creme de leite", "leite", "Alho frito", "Alho")]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.yaml"
            p.write_text(yaml.safe_dump({"respostas": [
                {"item": "leite", "texto": "3"}, {"item": "Alho", "texto": "1"},
            ]}, allow_unicode=True), encoding="utf-8")
            _apply_answers_file(planned, p)
        answered = {it["raw"]: it.get("user_answer") for it in planned}
        self.assertEqual(answered, {"Leite em pó": None, "Creme de leite": None, "leite": "3",
                                    "Alho frito": None, "Alho": "1"})


class SiteJobsTest(unittest.TestCase):
    """Roda os passos do site num diretório temporário, sem navegador."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = {k: getattr(server, k) for k in ("LIST", "RUN", "STATE", "ANSWERS", "ANSWERS_YAML", "MIX_HISTORY")}
        server.LIST, server.RUN = root / "lista.md", root / "run.json"
        server.STATE, server.ANSWERS = root / "state.json", root / "answers.json"
        server.MIX_HISTORY, server.ANSWERS_YAML = root / "mix.json", root / "respostas.yaml"
        server.LIST.write_text("Arroz\nFeijão\n", encoding="utf-8")
        server.RUN.write_text(json.dumps({"items": [
            {"raw": "Arroz", "status": "decided", "chosen_name": "Camil"},
        ]}), encoding="utf-8")
        server._set_state(andorinha_login=True, plan_list_hash=server._list_hash())
        self.calls = []
        server.JOB.run = lambda args, **kw: self.calls.append(args) or 0
        server.JOB.say = lambda line: None

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(server, k, v)
        del server.JOB.run, server.JOB.say  # volta aos métodos da classe
        self.tmp.cleanup()

    def test_cart_refuses_when_list_changed_after_search(self):
        server.LIST.write_text("Arroz\nFeijão\nBanana\n", encoding="utf-8")
        self.assertEqual(server._job_cart(), "lista")
        self.assertTrue(server._doubts()["stale"])
        self.assertEqual(self.calls, [])

    def test_cart_refuses_without_andorinha_login(self):
        server._set_state(andorinha_login=False)
        self.assertEqual(server._job_cart(), "login")
        self.assertEqual(self.calls, [])

    def test_cart_applies_when_everything_is_ready(self):
        self.assertEqual(server._job_cart(), "ok")
        self.assertEqual(self.calls[-1][3], "apply")

    def test_plan_refuses_empty_list(self):
        server.LIST.write_text("# Lista de Compras\n\n", encoding="utf-8")
        self.assertEqual(server._job_plan(), "vazia")
        self.assertEqual(self.calls, [])

    def test_free_text_answer_that_finds_nothing_comes_back_as_warning(self):
        server.RUN.write_text(json.dumps({"items": [
            {"raw": "Suquinho", "status": "needs_user", "candidates": []},
        ]}), encoding="utf-8")
        server._write_json(server.ANSWERS, {"Suquinho": {"texto": "zzqxw", "por": "voce"}})
        self.assertEqual(server._job_cart(), "faltam")  # o resolve falso não muda o run.json
        pending = server._doubts()["pending"]
        self.assertEqual((pending[0]["answer"], pending[0]["failed"]), ("", "zzqxw"))


if __name__ == "__main__":
    unittest.main()
