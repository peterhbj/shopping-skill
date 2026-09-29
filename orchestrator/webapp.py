"""Loopback-only shopping review app. Run: python -m orchestrator.webapp."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .decision.jev import compare_to_user_choice
from .fast_plan import plan_fast
from .history import PurchaseHistory, default_path
from .main import run
from .reviewer import make_reviewer
from .selector import packs_for_qty
from .site_history import sync_orders


ROOT = Path(__file__).resolve().parent.parent
UI = Path(__file__).resolve().parent / "ui"


class App:
    def __init__(self):
        self.data_dir = default_path().parent
        self.history = PurchaseHistory()
        self.reviewer = make_reviewer(ROOT, self.data_dir)
        self.token = secrets.token_urlsafe(32)
        self.list_path = ROOT / "lista-compras.md"
        self.profile_path = ROOT / "preferencias.yaml"
        self.run_file = self.data_dir / "run.json"
        self.report_path = self.data_dir / "relatorio.md"
        self.lock = threading.RLock()
        self.job: dict = {"stage": "idle", "running": False}
        self.plan: dict | None = None
        self.review = ""
        self.login_process: subprocess.Popen | None = None
        if self.run_file.exists():
            try:
                self.plan = json.loads(self.run_file.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass

    def start_job(self, label: str, action) -> None:
        with self.lock:
            if self.job.get("running"):
                raise ValueError("Já há uma operação em andamento")
            self.job = {"stage": label, "running": True, "error": None}
        def worker():
            try:
                action()
                with self.lock:
                    self.job.update({"stage": "complete", "running": False})
            except Exception as exc:
                with self.lock:
                    self.job.update({"stage": "error", "running": False, "error": str(exc)[:400]})
        threading.Thread(target=worker, daemon=True).start()

    def progress(self, event: dict) -> None:
        with self.lock:
            self.job.update(event)

    def save_plan(self) -> None:
        assert self.plan is not None
        self.run_file.write_text(json.dumps(self.plan, ensure_ascii=False, indent=2), encoding="utf-8")

    def require_browser_available(self) -> None:
        if self.login_process and self.login_process.poll() is None:
            raise ValueError("Conclua o login do Andorinha antes desta operação")


APP = App()


class Handler(BaseHTTPRequestHandler):
    server_version = "AndorinhaLocal/0.1"

    def log_message(self, format, *args):
        # Never log request bodies, tokens, order lines or API keys.
        pass

    def _response(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, value: dict) -> None:
        self._response(code, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > 6_000_000:
            raise ValueError("Requisição grande demais")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("JSON inválido")
        return data

    def _check_post(self) -> None:
        if self.headers.get("X-App-Token") != APP.token:
            raise PermissionError("Token local inválido")
        origin = self.headers.get("Origin")
        expected = f"http://127.0.0.1:{self.server.server_port}"
        if origin and origin != expected:
            raise PermissionError("Origem inválida")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            page = (UI / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", APP.token)
            self._response(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif path in ("/app.js", "/style.css"):
            kind = "application/javascript" if path.endswith(".js") else "text/css"
            self._response(200, (UI / path[1:]).read_bytes(), kind + "; charset=utf-8")
        elif path == "/api/state":
            with APP.lock:
                self._json(200, {"job": APP.job, "plan": APP.plan, "review": APP.review,
                                 "history": APP.history.counts(),
                                 "list": APP.list_path.read_text(encoding="utf-8"),
                                 "jev_ready": bool(os.getenv("TYPESAFE_API_KEY")),
                                 "andorinha_login_open": bool(APP.login_process and APP.login_process.poll() is None)})
        elif path == "/api/reviewer/status":
            try:
                self._json(200, APP.reviewer.status())
            except Exception as exc:
                self._json(503, {"error": str(exc)[:300]})
        else:
            self._json(404, {"error": "Não encontrado"})

    def do_POST(self):
        try:
            self._check_post()
            data = self._read_json()
            path = urlparse(self.path).path
            if path == "/api/jev/key":
                key = str(data.get("key") or "").strip()
                if key:
                    os.environ["TYPESAFE_API_KEY"] = key
                else:
                    os.environ.pop("TYPESAFE_API_KEY", None)
                self._json(200, {"ready": bool(key)})
            elif path == "/api/reviewer/login":
                if not getattr(APP.reviewer, "needs_login", False):
                    raise ValueError("O assistente atual usa o login do Claude CLI, não este botão")
                self._json(200, APP.reviewer.start_device_login())
            elif path == "/api/andorinha/login":
                if APP.login_process and APP.login_process.poll() is None:
                    raise ValueError("Janela de login já aberta")
                APP.login_process = subprocess.Popen(
                    [sys.executable, "-m", "orchestrator.browser", "--login"],
                    cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, text=True,
                )
                self._json(200, {"message": "Entre no Andorinha na janela aberta; depois clique em 'Login concluído'."})
            elif path == "/api/andorinha/login/done":
                if APP.login_process and APP.login_process.poll() is None and APP.login_process.stdin:
                    APP.login_process.stdin.write("\n")
                    APP.login_process.stdin.flush()
                    try:
                        APP.login_process.wait(timeout=10)
                    except subprocess.TimeoutExpired as exc:
                        raise ValueError("A janela de login ainda está fechando; tente novamente") from exc
                self._json(200, {"ok": True})
            elif path == "/api/history/import":
                name = Path(str(data.get("filename") or "orders.json")).name
                if Path(name).suffix.lower() not in (".csv", ".json"):
                    raise ValueError("Use JSON ou CSV")
                contents = str(data.get("contents") or "")
                import_path = APP.data_dir / ("import-" + secrets.token_hex(8) + Path(name).suffix.lower())
                try:
                    import_path.write_text(contents, encoding="utf-8")
                    result = APP.history.import_file(import_path)
                finally:
                    import_path.unlink(missing_ok=True)
                self._json(200, result)
            elif path == "/api/history/sync":
                APP.require_browser_available()
                APP.start_job("history", lambda: APP.progress(sync_orders(APP.history)))
                self._json(202, {"started": True})
            elif path == "/api/plan":
                APP.require_browser_available()
                list_text = str(data.get("list") or "")
                if not list_text.strip():
                    raise ValueError("Lista vazia")
                with APP.lock:
                    # Check before touching the list file: a second click must
                    # not replace the list behind a running job.
                    if APP.job.get("running"):
                        raise ValueError("Já há uma operação em andamento")
                    APP.list_path.write_text(list_text, encoding="utf-8")
                def work():
                    plan = plan_fast(APP.list_path, APP.profile_path, APP.history, APP.run_file,
                                     jev_shadow=True, progress=APP.progress)
                    with APP.lock:
                        APP.plan = plan
                        APP.review = ""
                    APP.progress({"stage": "review"})
                    try:
                        explanation = APP.reviewer.explain_exceptions(plan)
                    except Exception as exc:
                        explanation = f"Assistente indisponível: {str(exc)[:200]}. O plano continua para revisão manual."
                    with APP.lock:
                        APP.review = explanation
                APP.start_job("plan", work)
                self._json(202, {"started": True})
            elif path == "/api/resolve":
                with APP.lock:
                    if not APP.plan or APP.job.get("running"):
                        raise ValueError("Nenhum plano pronto")
                    index = int(data.get("index"))
                    item = APP.plan["items"][index]
                    if data.get("skip") is True:
                        item.update({"status": "not_found", "skipped_by_user": True,
                                     "chosen_name": None, "chosen_product_id": None,
                                     "notes": list(item.get("notes") or []) + ["Você decidiu pular"]})
                    else:
                        candidate_index = int(data.get("candidate_index"))
                        candidate = next((c for c in item.get("candidates") or [] if c.get("index") == candidate_index), None)
                        if candidate is None:
                            raise ValueError("Candidato inválido")
                        packs = packs_for_qty(item.get("qty") or 1, item.get("unit"))
                        price = float(candidate.get("price_num") or 0)
                        if price <= 0:
                            raise ValueError("Produto sem preço")
                        item.update({"status": "decided", "skipped_by_user": False,
                                     "chosen_index": candidate_index,
                                     "chosen_name": candidate["name"],
                                     "chosen_product_id": candidate.get("product_id"),
                                     "price_num": price, "packs_needed": packs,
                                     "total_cost": round(price * packs, 2),
                                     "rule": "user-choice",
                                     "notes": list(item.get("notes") or []) + ["Escolhido por você"]})
                        comparison = compare_to_user_choice(item.get("jev"), candidate_index)
                        if comparison:
                            item["jev_user_comparison"] = comparison
                        APP.history.correct(str(item.get("raw") or ""), candidate)
                    APP.save_plan()
                self._json(200, {"item": item})
            elif path == "/api/apply":
                APP.require_browser_available()
                with APP.lock:
                    plan = APP.plan
                    if not plan or APP.job.get("running"):
                        raise ValueError("Nenhum plano pronto")
                    if hashlib.sha256(APP.list_path.read_bytes()).hexdigest() != plan.get("meta", {}).get("list_sha256"):
                        raise ValueError("A lista mudou; gere outro plano")
                    pending = [i.get("raw") for i in plan.get("items") or []
                               if i.get("status") not in ("decided", "not_found") or
                               (i.get("status") == "not_found" and not i.get("skipped_by_user"))]
                    if pending:
                        raise ValueError(f"Resolva ou pule {len(pending)} item(ns) antes do carrinho")
                def work():
                    code = run(APP.list_path, APP.profile_path, APP.report_path,
                               command="apply", run_file=APP.run_file,
                               use_llm=False, keep_open=False)
                    if code:
                        raise RuntimeError(f"Aplicação do carrinho terminou com código {code}")
                    APP.progress({"report": APP.report_path.read_text(encoding="utf-8")})
                APP.start_job("apply", work)
                self._json(202, {"started": True})
            else:
                self._json(404, {"error": "Não encontrado"})
        except PermissionError as exc:
            self._json(403, {"error": str(exc)})
        except (ValueError, IndexError, KeyError) as exc:
            self._json(400, {"error": str(exc)[:300]})
        except Exception as exc:
            self._json(500, {"error": str(exc)[:300]})


def main() -> None:
    host, port = "127.0.0.1", int(os.getenv("ANDORINHA_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"Andorinha Shopping: {url}", flush=True)
    if os.getenv("ANDORINHA_NO_BROWSER") != "1":
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        APP.history.close()


if __name__ == "__main__":
    main()
