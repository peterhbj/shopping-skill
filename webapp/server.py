"""
server.py — site local das compras do Andorinha.

    python -m webapp.server        # abre http://127.0.0.1:8765

Roda só na máquina (127.0.0.1): o navegador do Andorinha, o Codex CLI e as
chaves ficam neste computador. Checkout e pagamento continuam manuais.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import re

import yaml

from orchestrator.enricher import extract_qty_from_raw, normalize
from orchestrator.llm.adapter import CodexCLIAdapter

from . import luna

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
LIST = ROOT / "lista-compras.md"
PROFILE = ROOT / "preferencias.yaml"
RUN = ROOT / "run.json"
REPORT = ROOT / "relatorio.md"
ENV_FILE = ROOT / ".env"
ANSWERS = ROOT / "respostas-site.json"
ANSWERS_YAML = ROOT / "respostas.yaml"
STATE = ROOT / ".site-state.json"
MIX_HISTORY = ROOT / "historico-sabores.json"
BROWSER_PROFILE = ROOT / ".andorinha-profile"
MODEL = os.getenv("SHOP_LLM_MODEL", "gpt-6-luna")
HOST, PORT = "127.0.0.1", int(os.getenv("SHOP_PORT", "8765"))


# ---------------------------------------------------------------- arquivos

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _jev_key() -> str | None:
    if os.getenv("TYPESAFE_API_KEY"):
        return os.environ["TYPESAFE_API_KEY"]
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("TYPESAFE_API_KEY="):
                return line.split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


def _save_jev_key(key: str) -> None:
    lines = []
    if ENV_FILE.is_file():
        lines = [l for l in ENV_FILE.read_text(encoding="utf-8").splitlines()
                 if not l.startswith("TYPESAFE_API_KEY=")]
    lines.append(f"TYPESAFE_API_KEY={key}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ENV_FILE.chmod(0o600)


def _child_env() -> dict:
    env = dict(os.environ)
    env.update({
        "PYTHONUNBUFFERED": "1",
        "ANDORINHA_PROFILE_DIR": str(BROWSER_PROFILE),
        "SHOP_LLM_PROVIDER": "codex",
        "SHOP_LLM_MODEL": MODEL,
    })
    key = _jev_key()
    if key:
        env["TYPESAFE_API_KEY"] = key
    return env


def _codex_logged_in() -> bool:
    if not shutil.which("codex"):
        return False
    try:
        r = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and "logged in" in (r.stdout + r.stderr).lower()


# ---------------------------------------------------------------- tarefas

class Job:
    """Uma tarefa por vez: todas usam o mesmo perfil do navegador."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.name: str | None = None
        self.running = False
        self.waiting_enter = False
        self.result: str | None = None
        self.log: list[str] = []
        self.proc: subprocess.Popen | None = None

    def snapshot(self, since: int) -> dict:
        return {
            "name": self.name, "running": self.running, "waiting_enter": self.waiting_enter,
            "result": self.result, "log": self.log[since:], "next": len(self.log),
        }

    def start(self, name: str, target) -> bool:
        with self.lock:
            if self.running:
                return False
            self.name, self.running, self.result = name, True, None
            self.waiting_enter = False
            self.log = []
        threading.Thread(target=self._wrap, args=(target,), daemon=True).start()
        return True

    def _wrap(self, target) -> None:
        try:
            self.result = target() or "ok"
        except Exception as e:  # noqa: BLE001 — mostrar qualquer falha na página
            self.say(f"[site] erro: {e}")
            self.result = "erro"
        finally:
            self.running = False
            self.waiting_enter = False
            self.proc = None

    def say(self, line: str) -> None:
        self.log.append(line.rstrip())

    def run(self, args: list[str], stdin: bool = False) -> int:
        shown = args[2:] if args[:2] == [sys.executable, "-m"] else args
        self.say("$ " + " ".join(shown))
        self.proc = subprocess.Popen(
            args, cwd=ROOT, env=_child_env(), text=True, bufsize=1,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
        )
        assert self.proc.stdout
        for line in self.proc.stdout:
            self.say(line)
            if "Pressione ENTER" in line or "keep_open=True" in line:
                self.waiting_enter = True
        return self.proc.wait()

    def press_enter(self) -> bool:
        p = self.proc
        if p and p.stdin and p.poll() is None:
            try:
                p.stdin.write("\n")
                p.stdin.flush()
                self.waiting_enter = False
                return True
            except OSError:
                pass
        return False


JOB = Job()


def _orch(command: str, *extra: str) -> list[str]:
    return [sys.executable, "-m", "orchestrator.main", command,
            "--list", str(LIST), "--profile", str(PROFILE),
            "--report-out", str(REPORT), *extra]


def _jev_flag() -> str:
    return "--jev-auto" if _jev_key() else "--no-jev"


def _job_login() -> str:
    JOB.say("[site] Abrindo o Andorinha. Entre com sua conta na janela que abriu "
            "e depois clique em “Já entrei” no card do Andorinha.")
    rc = JOB.run([sys.executable, "-m", "orchestrator.browser", "--login"], stdin=True)
    state = _read_json(STATE, {})
    state["andorinha_login"] = rc == 0
    _write_json(STATE, state)
    return "ok" if rc == 0 else "erro"


def _job_openai_login() -> str:
    JOB.say("[site] Abrindo o login da OpenAI no navegador…")
    rc = JOB.run(["codex", "login"])
    return "ok" if rc == 0 else "erro"


def _job_plan() -> str:
    rc = JOB.run(_orch("plan", "--no-llm", "--no-keep-open", _jev_flag()))
    if rc != 0:
        return "erro"
    answers: dict = {}
    if _codex_logged_in():
        adapter = CodexCLIAdapter(model=MODEL)
        for raw, idx in luna.run_luna(RUN, PROFILE, adapter, log=JOB.say).items():
            answers[raw] = {"texto": idx, "por": "luna"}
    else:
        JOB.say("[site] Sem login da OpenAI: o Luna não revisou as dúvidas.")
    _write_json(ANSWERS, answers)
    return "ok"


def _item_key(raw: str) -> str:
    """'Suquinho (12)' e 'suquinho (10)' → 'suquinho': o mix vale entre compras."""
    return normalize(extract_qty_from_raw(re.sub(r"\s*\([^)]*\)\s*$", "", raw or ""))[0])


def _remember_mix(raw: str, texto: str, run: dict) -> None:
    if not texto.lower().startswith("mix:"):
        return
    item = next((it for it in run.get("items") or [] if it.get("raw") == raw), None)
    if not item:
        return
    names = {c.get("index"): c.get("name") for c in item.get("candidates") or []}
    mix = {}
    for part in texto[4:].split(","):
        idx, _, n = part.partition("=")
        if idx.strip().isdigit() and n.strip().isdigit() and names.get(int(idx)):
            mix[names[int(idx)]] = int(n)
    if mix:
        history = _read_json(MIX_HISTORY, {})
        history[_item_key(raw)] = mix
        _write_json(MIX_HISTORY, history)


def _last_mix(raw: str, candidates: list[dict]) -> dict[int, int]:
    saved = _read_json(MIX_HISTORY, {}).get(_item_key(raw)) or {}
    return {c["index"]: saved[c["name"]] for c in candidates if c.get("name") in saved}


def _pending(run: dict) -> list[dict]:
    return [it for it in run.get("items") or [] if it.get("status") == "needs_user"]


def _job_cart() -> str:
    answers = _read_json(ANSWERS, {})
    run = _read_json(RUN, {})
    if not run:
        JOB.say("[site] Faça a busca de teste primeiro.")
        return "erro"
    if answers and _pending(run):
        rows = [{"item": raw, "texto": a["texto"]} for raw, a in answers.items() if a.get("texto")]
        ANSWERS_YAML.write_text(
            yaml.safe_dump({"respostas": rows}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        if JOB.run(_orch("resolve", "--no-keep-open", "--answers", str(ANSWERS_YAML))) != 0:
            return "erro"
        run = _read_json(RUN, {})
    left = _pending(run)
    if left:
        JOB.say(f"[site] Ainda faltam {len(left)} dúvida(s). Responda e tente de novo.")
        return "faltam"
    JOB.say("[site] Adicionando ao carrinho. A janela fica aberta para você finalizar.")
    rc = JOB.run(_orch("apply", "--no-llm"), stdin=True)
    return "ok" if rc == 0 else "erro"


# ---------------------------------------------------------------- HTTP

def _doubts() -> dict:
    run = _read_json(RUN, {})
    answers = _read_json(ANSWERS, {})
    pend, auto, missing, decided = [], [], [], []
    for it in run.get("items") or []:
        status = it.get("status")
        if status == "not_found":
            missing.append({"raw": it.get("raw"), "search": it.get("search_term")})
        elif status in ("decided", "ambiguous_resolved"):
            decided.append({"raw": it.get("raw"), "name": it.get("chosen_name"),
                            "total": it.get("total_cost"), "rule": it.get("rule")})
        elif status == "needs_user":
            a = answers.get(it.get("raw")) or {}
            entry = {
                "raw": it.get("raw"), "qty": it.get("qty"), "unit": it.get("unit"),
                "notes": it.get("notes") or [],
                "candidates": [
                    {"index": c.get("index"), "name": c.get("name"), "price": c.get("price_num"),
                     "per": c.get("price_per_base_unit"), "base": c.get("price_base_dim")}
                    for c in (it.get("candidates") or [])[:luna.MAX_CANDIDATES]
                ],
                "luna": it.get("luna"), "answer": a.get("texto"), "by": a.get("por"),
            }
            entry["last_mix"] = _last_mix(it.get("raw"), entry["candidates"])
            (auto if a.get("por") == "luna" else pend).append(entry)
    return {"has_run": bool(run), "pending": pend, "auto": auto,
            "missing": missing, "decided": decided}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:
        pass

    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return origin in (None, f"http://{HOST}:{PORT}", f"http://localhost:{PORT}")

    def _local_host(self) -> bool:
        # Barra DNS rebinding: um site externo apontado para 127.0.0.1 chega
        # com o próprio domínio no Host.
        return self.headers.get("Host") in (f"{HOST}:{PORT}", f"localhost:{PORT}")

    def do_GET(self) -> None:
        if not self._local_host():
            return self._send(403, {"error": "host não permitido"})
        path, _, query = self.path.partition("?")
        if path == "/":
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html")
        if path == "/api/status":
            state = _read_json(STATE, {})
            return self._send(200, {
                "andorinha": bool(state.get("andorinha_login")),
                "openai": _codex_logged_in(),
                "jev": bool(_jev_key()),
                "model": MODEL,
            })
        if path == "/api/list":
            text = LIST.read_text(encoding="utf-8") if LIST.is_file() else "# Lista de Compras\n"
            return self._send(200, {"text": text})
        if path == "/api/job":
            since = int(query.split("=", 1)[1]) if query.startswith("since=") else 0
            return self._send(200, JOB.snapshot(since))
        if path == "/api/doubts":
            return self._send(200, _doubts())
        self._send(404, {"error": "não encontrado"})

    def do_POST(self) -> None:
        if not (self._local_host() and self._same_origin()):
            return self._send(403, {"error": "origem não permitida"})
        body = self._body()
        jobs = {
            "/api/andorinha/login": ("login", _job_login),
            "/api/openai/login": ("openai", _job_openai_login),
            "/api/plan": ("plan", _job_plan),
            "/api/cart": ("cart", _job_cart),
        }
        if self.path in jobs:
            if self.path == "/api/openai/login" and not shutil.which("codex"):
                return self._send(400, {"error": "Codex CLI não instalado"})
            ok = JOB.start(*jobs[self.path])
            return self._send(200 if ok else 409,
                              {"ok": ok, "error": None if ok else "já tem uma tarefa rodando"})
        if self.path == "/api/job/enter":
            return self._send(200, {"ok": JOB.press_enter()})
        if self.path == "/api/jev":
            key = str(body.get("key") or "").strip()
            if not key or any(c.isspace() for c in key):
                return self._send(400, {"error": "chave inválida"})
            _save_jev_key(key)
            return self._send(200, {"ok": True})
        if self.path == "/api/list":
            if JOB.running and JOB.name in ("plan", "cart"):
                return self._send(409, {"error": "espere a tarefa terminar"})
            LIST.write_text(str(body.get("text") or ""), encoding="utf-8")
            return self._send(200, {"ok": True})
        if self.path == "/api/answers":
            answers = _read_json(ANSWERS, {})
            run = _read_json(RUN, {})
            for raw, texto in (body.get("answers") or {}).items():
                texto = str(texto or "").strip()
                _remember_mix(raw, texto, run)
                if texto and (answers.get(raw) or {}).get("texto") != texto:
                    answers[raw] = {"texto": texto, "por": "voce"}
                elif texto:
                    continue
                else:
                    answers.pop(raw, None)
            _write_json(ANSWERS, answers)
            return self._send(200, {"ok": True})
        self._send(404, {"error": "não encontrado"})


def main() -> int:
    BROWSER_PROFILE.mkdir(mode=0o700, exist_ok=True)
    BROWSER_PROFILE.chmod(0o700)  # cookies da sessão do Andorinha
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"[site] aberto em {url} — Ctrl+C para fechar", flush=True)
    if "--no-open" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
