"""Minimal Codex app-server client, isolated from the desktop app account."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from .reviewer import NO_EXCEPTIONS, build_review_prompt


class LunaUnavailable(RuntimeError):
    pass


class LunaClient:
    provider = "luna"
    needs_login = True

    def __init__(self, project: Path, data_dir: Path):
        self.project = project
        self.codex_home = data_dir / "codex-home-personal"
        self.codex_home.mkdir(parents=True, exist_ok=True)
        self.process: subprocess.Popen | None = None
        self.messages: queue.Queue[dict] = queue.Queue()
        self.counter = 0
        self.lock = threading.Lock()

    def _start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.codex_home)
        try:
            self.process = subprocess.Popen(
                ["codex", "app-server", "--stdio"], cwd=self.project, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1,
            )
        except OSError as exc:
            raise LunaUnavailable(f"Codex CLI indisponível: {exc}") from exc
        def reader():
            assert self.process and self.process.stdout
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
        threading.Thread(target=reader, daemon=True).start()
        self._send("initialize", {"clientInfo": {"name": "andorinha-shopping", "title": "Andorinha Shopping", "version": "0.1.0"}}, timeout=15)
        self._write({"method": "initialized", "params": {}})

    def _write(self, payload: dict) -> None:
        if not self.process or not self.process.stdin or self.process.poll() is not None:
            raise LunaUnavailable("Codex app-server parou")
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _send(self, method: str, params: dict, timeout: int = 20) -> dict:
        self.counter += 1
        call_id = self.counter
        self._write({"method": method, "params": params, "id": call_id})
        deadline = time.monotonic() + timeout
        deferred = []
        try:
            while time.monotonic() < deadline:
                try:
                    message = self.messages.get(timeout=min(1, max(0.01, deadline - time.monotonic())))
                except queue.Empty:
                    if self.process and self.process.poll() is not None:
                        break
                    continue
                if message.get("id") == call_id:
                    if "error" in message:
                        raise LunaUnavailable(str(message["error"]))
                    return message.get("result") or {}
                deferred.append(message)
        finally:
            for message in deferred:
                self.messages.put(message)
        raise LunaUnavailable(f"Sem resposta do Codex app-server para {method}")

    def account(self) -> dict:
        with self.lock:
            self._start()
            return self._send("account/read", {})

    def status(self) -> dict:
        account = (self.account().get("account") or {})
        ready = account.get("type") == "chatgpt"
        label = f"Luna: {account.get('email') or 'ChatGPT pessoal'}" if ready else "Luna: conta pessoal não conectada"
        return {"provider": self.provider, "ready": ready, "label": label, "needs_login": True}

    def start_device_login(self) -> dict:
        with self.lock:
            self._start()
            return self._send("account/login/start", {"type": "chatgptDeviceCode"})

    def explain_exceptions(self, plan: dict) -> str:
        prompt = build_review_prompt(plan)
        if prompt is None:
            return NO_EXCEPTIONS
        with self.lock:
            self._start()
            account = self._send("account/read", {})
            if (account.get("account") or {}).get("type") != "chatgpt":
                raise LunaUnavailable("Entre com a conta pessoal do ChatGPT no aplicativo")
            thread = self._send("thread/start", {
                "model": "gpt-6-luna", "cwd": str(self.project),
                "approvalPolicy": "never", "sandbox": "readOnly",
                "serviceName": "andorinha_shopping",
            })
            thread_id = (thread.get("thread") or {}).get("id")
            if not thread_id:
                raise LunaUnavailable("Codex não iniciou uma conversa")
            self._send("turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "cwd": str(self.project), "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly"}, "model": "gpt-6-luna",
            })
            parts: list[str] = []
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    event = self.messages.get(timeout=1)
                except queue.Empty:
                    continue
                method = event.get("method")
                params = event.get("params") or {}
                if method == "item/agentMessage/delta":
                    parts.append(str(params.get("delta") or ""))
                if method == "item/completed":
                    item = params.get("item") or {}
                    if item.get("type") == "agentMessage" and item.get("text"):
                        parts = [str(item["text"])]
                if method == "turn/completed":
                    if (params.get("turn") or {}).get("status") == "failed":
                        raise LunaUnavailable("Luna não concluiu a revisão")
                    return "".join(parts).strip() or "Luna terminou sem explicações."
            raise LunaUnavailable("Tempo esgotado na revisão do Luna")
