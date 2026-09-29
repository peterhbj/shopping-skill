import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.reviewer import (NO_EXCEPTIONS, ClaudeReviewer, ReviewerUnavailable,
                                   build_review_prompt, make_reviewer)

PLAN = {"items": [
    {"raw": "arroz", "status": "decided"},
    {"raw": "feijão", "status": "needs_user", "notes": ["ambíguo"],
     "candidates": [{"index": 0, "name": "Feijão Camil 1kg", "price_num": 8.5}]},
]}


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class PromptTest(unittest.TestCase):
    def test_only_open_exceptions_are_sent(self):
        prompt = build_review_prompt(PLAN)
        self.assertIn("feijão", prompt)
        self.assertNotIn("arroz", prompt)

    def test_no_exceptions_means_no_call(self):
        self.assertIsNone(build_review_prompt({"items": [{"raw": "a", "status": "decided"}]}))
        with patch("orchestrator.reviewer.subprocess.run") as run:
            reviewer = ClaudeReviewer(Path("."))
            self.assertEqual(reviewer.explain_exceptions({"items": []}), NO_EXCEPTIONS)
            run.assert_not_called()


class ClaudeReviewerTest(unittest.TestCase):
    def setUp(self):
        self.reviewer = ClaudeReviewer(Path("."))
        patcher = patch("orchestrator.reviewer.shutil.which", return_value="/bin/claude")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_calls_haiku_without_tools_and_sends_prompt_on_stdin(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-x"}), \
             patch("orchestrator.reviewer.subprocess.run",
                   return_value=completed(json.dumps({"result": " Escolha o 0. "}))) as run:
            self.assertEqual(self.reviewer.explain_exceptions(PLAN), "Escolha o 0.")
        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ["-p", "--model"])
        self.assertEqual(command[3], "haiku")
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertIn("feijão", run.call_args.kwargs["input"])
        self.assertNotIn("feijão", " ".join(command))
        self.assertNotIn("ANTHROPIC_API_KEY", run.call_args.kwargs["env"])

    def test_errors_become_reviewer_unavailable(self):
        cases = [completed("", 1, "not logged in"),
                 completed(json.dumps({"is_error": True, "result": "Invalid API key"}))]
        for case in cases:
            with patch("orchestrator.reviewer.subprocess.run", return_value=case), \
                 self.assertRaises(ReviewerUnavailable):
                self.reviewer.explain_exceptions(PLAN)
        with patch("orchestrator.reviewer.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("claude", 90)), \
             self.assertRaises(ReviewerUnavailable):
            self.reviewer.explain_exceptions(PLAN)

    def test_status_reports_missing_cli(self):
        with patch("orchestrator.reviewer.shutil.which", return_value=None):
            self.assertFalse(self.reviewer.status()["ready"])
        self.assertTrue(self.reviewer.status()["ready"])


class FactoryTest(unittest.TestCase):
    def test_claude_is_default_and_luna_is_opt_in(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("ANDORINHA_REVIEWER", None)
            self.assertEqual(make_reviewer(Path("."), Path(".")).provider, "claude")
        import tempfile
        with tempfile.TemporaryDirectory() as temp, \
             patch.dict("os.environ", {"ANDORINHA_REVIEWER": "luna"}):
            self.assertEqual(make_reviewer(Path("."), Path(temp)).provider, "luna")


if __name__ == "__main__":
    unittest.main()
