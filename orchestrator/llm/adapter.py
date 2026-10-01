"""
adapter.py — Adapter para invocar uma LLM (opcional).

ClaudeCodeCLIAdapter: subprocess do `claude -p` isolado.
Pode ser trocado por outro adapter no futuro.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional


class LLMCallError(Exception):
    pass


class LLMAdapter(ABC):
    @abstractmethod
    def ask(self, prompt: str) -> str:
        ...

    def ask_json(self, prompt: str, schema_hint: Optional[dict] = None) -> dict:
        instr = (
            "\n\nResponda APENAS com um objeto JSON válido, sem texto adicional, "
            "sem markdown, sem code fences."
        )
        if schema_hint:
            instr += f"\nSchema esperado: {json.dumps(schema_hint, ensure_ascii=False)}"
        raw = self.ask(prompt + instr)
        return _parse_json_tolerant(raw)


def _parse_json_tolerant(raw: str) -> dict:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    obj_match = re.search(r"\{.*\}", text, re.DOTALL)
    if obj_match:
        try:
            return json.loads(obj_match.group(0))
        except json.JSONDecodeError as e:
            raise LLMCallError(f"Resposta não é JSON parseável: {raw[:200]!r}") from e
    raise LLMCallError(f"Resposta não contém JSON: {raw[:200]!r}")


class GrokCLIAdapter(LLMAdapter):
    """Headless Grok Build: `grok -p` (substitui o claude -p)."""

    _DISALLOWED = (
        "run_terminal_cmd,web_search,web_fetch,search_replace,"
        "read_file,grep,list_dir,Agent"
    )

    def __init__(
        self,
        model: str | None = None,
        timeout_s: int = 120,
        extra_args: Optional[list[str]] = None,
        grok_bin: Optional[str] = None,
    ):
        allowed = {"grok-4.6", "grok-4.5"}
        if model in allowed:
            self.model = model
        else:
            # default do CLI (hoje: grok-4.6). Não passar -m grok-build.
            self.model = None
        self.timeout = timeout_s
        self.extra_args = extra_args or []
        self.grok_bin = grok_bin or self._resolve_grok_bin()

    @staticmethod
    def _resolve_grok_bin() -> str:
        found = shutil.which("grok")
        if found:
            return found
        home = Path(os.path.expanduser("~")) / ".grok" / "bin"
        for name in ("grok.exe", "grok"):
            p = home / name
            if p.is_file():
                return str(p)
        return "grok"

    def ask(self, prompt: str) -> str:
        args = [
            self.grok_bin,
            "-p", prompt,
            "--output-format", "json",
            "--verbatim",
            "--max-turns", "1",
            "--disallowed-tools", self._DISALLOWED,
        ]
        if self.model:
            args += ["-m", self.model]
        args += self.extra_args
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=tempfile.gettempdir(),
            )
        except subprocess.TimeoutExpired as e:
            raise LLMCallError(f"grok CLI timeout após {self.timeout}s") from e
        except FileNotFoundError as e:
            raise LLMCallError(
                "grok CLI não encontrado. Instale o Grok Build e rode `grok login`."
            ) from e
        if result.returncode != 0:
            raise LLMCallError(
                f"grok CLI rc={result.returncode}\nstderr: {(result.stderr or '')[:400]}"
            )
        raw = (result.stdout or "").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(data, dict) and data.get("type") == "error":
            raise LLMCallError(f"grok CLI: {data.get('message', data)}")
        if isinstance(data, dict) and "text" in data:
            return str(data["text"]).strip()
        return raw


class CodexCLIAdapter(LLMAdapter):
    """`codex exec` headless, usando o login do ChatGPT do Codex CLI (OAuth)."""

    def __init__(
        self,
        model: str = "gpt-6-luna",
        timeout_s: int = 300,
        reasoning_effort: str = "low",
        codex_bin: Optional[str] = None,
    ):
        self.model = model
        self.timeout = timeout_s
        self.reasoning_effort = reasoning_effort
        self.codex_bin = codex_bin or shutil.which("codex") or "codex"

    def ask(self, prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix="shop-codex-") as tmp:
            out = Path(tmp) / "last.txt"
            args = [
                self.codex_bin, "exec",
                "-m", self.model,
                "-s", "read-only",
                "--skip-git-repo-check",
                "--ephemeral",
                "-c", f"model_reasoning_effort={self.reasoning_effort}",
                "-o", str(out),
                prompt,
            ]
            try:
                result = subprocess.run(
                    args, capture_output=True, text=True,
                    timeout=self.timeout, cwd=tmp, stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as e:
                raise LLMCallError(f"codex CLI timeout após {self.timeout}s") from e
            except FileNotFoundError as e:
                raise LLMCallError(
                    "codex CLI não encontrado. Instale o Codex CLI e rode `codex login`."
                ) from e
            if result.returncode != 0:
                raise LLMCallError(
                    f"codex CLI rc={result.returncode}\nstderr: {(result.stderr or '')[-400:]}"
                )
            if out.is_file():
                return out.read_text(encoding="utf-8").strip()
            return (result.stdout or "").strip()


def make_adapter(raw_cfg: dict | None = None) -> LLMAdapter:
    """Escolhe o adapter: env SHOP_LLM_PROVIDER/SHOP_LLM_MODEL > perfil > grok."""
    raw_cfg = raw_cfg or {}
    provider = (os.getenv("SHOP_LLM_PROVIDER") or raw_cfg.get("llm_provider") or "grok").lower()
    model = os.getenv("SHOP_LLM_MODEL") or raw_cfg.get("llm_model")
    timeout = int(raw_cfg.get("llm_timeout_s", 120))
    if provider == "codex":
        return CodexCLIAdapter(model=model or "gpt-6-luna", timeout_s=max(timeout, 300))
    return GrokCLIAdapter(model=model or "grok-4.6", timeout_s=timeout)


class ClaudeCodeCLIAdapter(LLMAdapter):
    def __init__(
        self,
        model: str = "haiku",
        timeout_s: int = 120,
        extra_args: Optional[list[str]] = None,
        use_shell: bool = False,
        claude_bin: Optional[str] = None,
    ):
        self.model = model
        self.timeout = timeout_s
        self.extra_args = extra_args or []
        self.use_shell = use_shell
        self.claude_bin = claude_bin or self._resolve_claude_bin()

    @staticmethod
    def _resolve_claude_bin() -> str:
        candidates = [
            os.path.expanduser("~/.local/bin/claude"),
            "/opt/homebrew/bin/claude",
            "/usr/local/bin/claude",
        ]
        for c in candidates:
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        return "claude"

    def ask(self, prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix="shop-llm-") as tmp:
            env = os.environ.copy()
            base_args = [
                self.claude_bin,
                "-p", prompt,
                "--model", self.model,
                "--output-format", "text",
                "--disallowedTools", "*",
            ] + self.extra_args

            try:
                if self.use_shell:
                    cmd_str = " ".join(_shell_quote(a) for a in base_args)
                    result = subprocess.run(
                        cmd_str, shell=True, executable="/bin/zsh",
                        capture_output=True, text=True,
                        timeout=self.timeout, cwd=tmp, env=env,
                    )
                else:
                    result = subprocess.run(
                        base_args,
                        capture_output=True, text=True,
                        timeout=self.timeout, cwd=tmp, env=env,
                    )
            except subprocess.TimeoutExpired as e:
                raise LLMCallError(f"claude CLI timeout após {self.timeout}s") from e
            except FileNotFoundError as e:
                raise LLMCallError(
                    "claude CLI não encontrado no PATH. "
                    "Tente use_shell=True se for alias do shell."
                ) from e

        if result.returncode != 0:
            raise LLMCallError(
                f"claude CLI rc={result.returncode}\nstderr: {result.stderr[:400]}"
            )
        return result.stdout.strip()


def _shell_quote(arg: str) -> str:
    if not arg or any(c in arg for c in " '\"\\$`!*?[]{}()<>|&;"):
        return "'" + arg.replace("'", "'\\''") + "'"
    return arg


def render_ambiguity_prompt(
    template_path: Path, ambiguity, item_text: str, qty: int, user_answer: str = ""
) -> str:
    template = template_path.read_text(encoding="utf-8")
    pref = ambiguity.preferred
    alts = [c for c in ambiguity.candidates if pref is None or c.index != pref.index]
    pref_str = (
        f"- index {pref.index}: {pref.name} — R${pref.price_num:.2f} "
        f"({pref.price_per_base_unit:.4f}/{pref.price_base_dim})"
        if pref else "(nenhuma marca preferida explícita)"
    )
    alts_str = "\n".join(
        f"- index {c.index}: {c.name} — R${c.price_num:.2f} "
        f"({c.price_per_base_unit:.4f}/{c.price_base_dim})"
        for c in alts
    )
    return template.format(
        item_text=item_text,
        qty=qty,
        reason=ambiguity.reason,
        preferred=pref_str,
        alternatives=alts_str,
        user_answer=user_answer.strip() or "(o cliente ainda não falou)",
    )
