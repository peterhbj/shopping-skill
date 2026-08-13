"""
browser.py — Playwright wrapper para Andorinha Online.

Conecta ao Chrome rodando em CDP (localhost:9222), injeta os helpers JS
uma vez (add_init_script) e expõe API alto-nível:

    with Browser(cdp_url) as b:
        b.search("papel higiênico")
        results = b.get_results()  # já com price_per_base_unit
        b.set_qty(index=2, target_qty=3)
        badge = b.cart_badge()

A função set_qty resolve o bug de debounce da v1: clica increment/decrement
com await de mutação do DOM entre cada click (timeout configurável).
"""

from __future__ import annotations

import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import sync_playwright, Page, Browser as PWBrowser


HELPERS_PATH = Path(__file__).resolve().parent.parent / "scripts" / "browser_helpers.js"


@dataclass
class ProductResult:
    index: int
    name: str
    name_lower: str
    price_text: str
    price_num: float
    unit: str
    unit_count: Optional[int]
    weight_g: Optional[float]
    volume_ml: Optional[float]
    price_per_base_unit: float
    price_base_dim: str
    price_per_kg: bool
    in_cart: bool
    current_qty: int
    has_add: bool
    has_plus: bool
    has_minus: bool

    @classmethod
    def from_dict(cls, d: dict) -> "ProductResult":
        return cls(
            index=d["index"],
            name=d["name"],
            name_lower=d["name_lower"],
            price_text=d["price_text"],
            price_num=d["price_num"],
            unit=d["unit"],
            unit_count=d.get("unit_count"),
            weight_g=d.get("weight_g"),
            volume_ml=d.get("volume_ml"),
            price_per_base_unit=d.get("price_per_base_unit", d["price_num"]),
            price_base_dim=d.get("price_base_dim", "un"),
            price_per_kg=d.get("price_per_kg", False),
            in_cart=d.get("in_cart", False),
            current_qty=d.get("current_qty", 0),
            has_add=d.get("has_add", False),
            has_plus=d.get("has_plus", False),
            has_minus=d.get("has_minus", False),
        )


class BrowserError(Exception):
    pass


class Browser:
    """
    Conecta ao Chrome via CDP. NÃO inicia Chrome próprio.
    Reusa sessão logada do usuário (cookies, carrinho preexistente, etc.).
    """

    BASE_URL = "https://www.andorinhaonline.com.br"

    def __init__(
        self,
        cdp_url: str = "http://localhost:9222",
        click_settle_ms: int = 250,
        nav_timeout_ms: int = 15000,
        page_index: int = 0,
        launch_own: bool = False,
        keep_open: bool = False,
        user_data_dir: Optional[str] = None,
        headless: bool = False,
    ):
        """
        Por padrão tenta conectar via CDP ao Chrome em `cdp_url`.

        Se launch_own=True (ou se CDP rejeitar context management), lança
        Chromium próprio do Playwright com perfil persistente em user_data_dir
        (default: /tmp/andorinha-pw-profile). Use launch_own quando o Chrome
        existente foi iniciado por outro processo (ex: MCP) e não suporta
        gerenciamento de contextos pelo Playwright.
        """
        self.cdp_url = cdp_url
        self.click_settle_ms = click_settle_ms
        self.nav_timeout_ms = nav_timeout_ms
        self.page_index = page_index
        self.launch_own = launch_own
        self.user_data_dir = user_data_dir or str(Path(tempfile.gettempdir()) / "andorinha-pw-profile")
        self.headless = headless
        self.keep_open = keep_open
        self._pw = None
        self._browser: Optional[PWBrowser] = None
        self._page: Optional[Page] = None
        self._helpers_js = HELPERS_PATH.read_text(encoding="utf-8")

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self) -> "Browser":
        self.start()
        return self

    def __exit__(self, *exc):
        if getattr(self, "keep_open", False):
            print("[browser] keep_open=True — browser permanece aberto. Feche a janela quando terminar a compra.", flush=True)
            try:
                input("[browser] Pressione ENTER aqui quando terminar de revisar o carrinho... ")
            except EOFError:
                import time
                print("[browser] (sem stdin) aguardando 30 min antes de fechar...", flush=True)
                time.sleep(1800)
        self.close()

    def start(self) -> None:
        self._pw = sync_playwright().start()

        if self.launch_own:
            self._start_own()
        else:
            try:
                self._start_cdp()
            except BrowserError as e:
                # Fallback automático para chromium próprio se CDP recusar.
                msg = str(e)
                if "context management is not supported" in msg.lower():
                    print(
                        f"[browser] CDP rejeitou (Chrome iniciado por outro processo). "
                        f"Caindo para launch próprio em {self.user_data_dir}",
                    )
                    self._start_own()
                else:
                    raise

        # add_init_script aplica-se a TODA navegação subsequente.
        # Também tentamos injetar na página atual (pode estar em estado transitório).
        if self._page:
            try:
                self._page.context.add_init_script(self._helpers_js)
            except Exception:
                pass
            try:
                self._page.evaluate(self._helpers_js)
            except Exception:
                pass
            self._page.set_default_timeout(self.nav_timeout_ms)

    def _start_cdp(self) -> None:
        try:
            self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        except Exception as e:
            raise BrowserError(
                f"Falha ao conectar Chrome em {self.cdp_url}: {e}"
            ) from e

        contexts = self._browser.contexts
        if not contexts:
            raise BrowserError("Nenhum browser context disponível via CDP.")
        ctx = contexts[0]

        pages = ctx.pages
        andorinha_pages = [p for p in pages if "andorinhaonline.com.br" in (p.url or "")]
        if andorinha_pages:
            self._page = andorinha_pages[0]
        elif pages:
            self._page = pages[self.page_index if self.page_index < len(pages) else 0]
        else:
            self._page = ctx.new_page()

    def _start_own(self) -> None:
        ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            headless=self.headless,
            viewport={"width": 1280, "height": 800},
        )
        # Em launch_persistent_context não há `browser` object; guardamos o context.
        self._browser = None  # type: ignore[assignment]
        self._page = ctx.pages[0] if ctx.pages else ctx.new_page()

    def close(self) -> None:
        # Não fechamos o browser (é o do usuário). Apenas desconectamos.
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = None
        self._browser = None
        self._page = None

    # ---------------------------------------------------------------------- nav

    @property
    def page(self) -> Page:
        if not self._page:
            raise BrowserError("Browser não iniciado. Chame start() ou use `with Browser(...)`.")
        return self._page

    def goto_home(self) -> None:
        self.page.goto(self.BASE_URL, wait_until="domcontentloaded")
        self._ensure_helpers()

    def search(self, query: str) -> None:
        # Já usa URL direta (/busca/...), não a barra de pesquisa.
        url = f"{self.BASE_URL}/busca/{urllib.parse.quote(query)}"
        print(f"[browser] goto {url}", flush=True)
        prev_url = ""
        try:
            prev_url = self.page.url or ""
        except Exception:
            pass
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
        except Exception as e:
            current = ""
            try:
                current = self.page.url or ""
            except Exception:
                pass
            # Timeout depois do SPA hidratar ainda é navegação válida.
            if "/busca/" in current and current != prev_url:
                print(f"[browser] goto timeout mas URL de busca carregou: {e}", flush=True)
            else:
                raise BrowserError(f"falha ao abrir busca {query!r}: {e}") from e

        self._ensure_helpers()
        self._wait_for_results()

    def _wait_for_results(self, timeout_ms: int = 8000) -> None:
        """
        SPA do Andorinha só hidrata os cards depois do JS.
        Sai cedo se a página disser '0 itens' / 'Encontramos 0'.
        """
        selectors = [
            ".item-product-wrapper",
            "[class*='item-product']",
            "[class*='product-card']",
            "[class*='ProductCard']",
        ]
        deadline = time.time() + (timeout_ms / 1000.0)
        while time.time() < deadline:
            # zero results message → abort wait
            try:
                body = (self.page.inner_text("body") or "")[:2000].lower()
                # Não usar "0 itens": o badge do carrinho vazio casa isso e aborta a busca.
                if "encontramos 0" in body or "nenhum produto" in body:
                    print("[browser] página indica 0 resultados — skip wait", flush=True)
                    return
            except Exception:
                pass
            for sel in selectors:
                try:
                    loc = self.page.locator(sel)
                    if loc.count() > 0:
                        self.page.wait_for_timeout(400)
                        print(f"[browser] cards via '{sel}' (count≈{loc.count()})", flush=True)
                        return
                except Exception:
                    continue
            self.page.wait_for_timeout(250)
        print("[browser] nenhum card de produto detectado após espera", flush=True)

    def _ensure_helpers(self) -> None:
        """Re-injeta helpers caso add_init_script não tenha pegado a página."""
        try:
            loaded = self.page.evaluate("() => !!window.__ANDORINHA_HELPERS_V3__")
        except Exception:
            loaded = False
        if not loaded:
            try:
                self.page.evaluate(self._helpers_js)
                print("[browser] helpers injetados via evaluate", flush=True)
            except Exception as e:
                print(f"[browser] falha ao injetar helpers: {e}", flush=True)

    # ---------------------------------------------------------------------- API

    def get_results(self) -> list[ProductResult]:
        self._ensure_helpers()
        try:
            payload = self.page.evaluate("() => andorinha_get_results()")
        except Exception as e:
            print(f"[browser] get_results evaluate error: {e}", flush=True)
            return []
        if isinstance(payload, dict) and payload.get("error"):
            print(f"[browser] get_results: {payload.get('error')} url={payload.get('url')}", flush=True)
            return []
        results = [ProductResult.from_dict(r) for r in (payload or {}).get("results", [])]
        print(f"[browser] get_results → {len(results)} produtos", flush=True)
        if results:
            r0 = results[0]
            print(
                f"[browser]   [0] {r0.name[:50]!r} R${r0.price_num} "
                f"add={r0.has_add} plus={r0.has_plus} minus={r0.has_minus}",
                flush=True,
            )
        return results

    def cart_badge(self) -> int:
        payload = self.page.evaluate("() => andorinha_get_cart_badge()")
        if isinstance(payload, dict) and "qty" in payload:
            return int(payload["qty"])
        return 0

    def current_qty(self, index: int) -> int:
        return int(self.page.evaluate(f"() => andorinha_get_current_qty({index})"))

    def set_qty(self, index: int, target_qty: int, max_attempts_per_step: int = 8) -> dict:
        """
        Adiciona clicando N vezes e confere se qty/badge mudaram.
        Não trata clique sem efeito como sucesso (login ausente, seletor quebrado).
        """
        self._ensure_helpers()
        target_qty = max(1, int(target_qty))
        steps_taken = 0

        try:
            badge_before = self.cart_badge()
        except Exception:
            badge_before = None
        try:
            qty_before = self.current_qty(index)
        except Exception:
            qty_before = None

        for step in range(target_qty):
            result = self.page.evaluate(f"() => andorinha_click_increase({index})")
            steps_taken += 1
            if isinstance(result, dict) and result.get("error"):
                print(f"[browser] set_qty step {step+1}/{target_qty} error: {result}", flush=True)
                self._ensure_helpers()
                result = self.page.evaluate(f"() => andorinha_click_increase({index})")
                if isinstance(result, dict) and result.get("error"):
                    return {
                        "success": False,
                        "final_qty": step,
                        "target_qty": target_qty,
                        "steps": steps_taken,
                        "error": result.get("error"),
                    }
            prev = result.get("prev_qty") if isinstance(result, dict) else None
            if isinstance(prev, int):
                self._await_qty_mutation(index, prev, max_attempts_per_step)
            # O card muda na hora (UI otimista); o POST do carrinho chega depois.
            # Sem pausa extra, o servidor descarta cliques em sequência.
            settle = max(self.click_settle_ms, 1000)
            if step == 0:
                settle = max(settle, 1500)
            self.page.wait_for_timeout(settle)

        self.page.wait_for_timeout(max(self.click_settle_ms, 1500))

        try:
            badge = self.cart_badge()
        except Exception:
            badge = None
        try:
            qty_now = self.current_qty(index)
        except Exception:
            qty_now = None

        badge_grew = (
            badge_before is not None
            and badge is not None
            and badge > badge_before
        )
        qty_grew = (
            qty_now is not None
            and qty_now > 0
            and (qty_before is None or qty_now > qty_before)
        )
        qty_reached = qty_now is not None and qty_now >= target_qty

        if not (badge_grew or qty_grew or qty_reached):
            print(
                f"[browser] set_qty SEM EFEITO index={index} clicks={steps_taken} "
                f"qty {qty_before}→{qty_now} badge {badge_before}→{badge}",
                flush=True,
            )
            return {
                "success": False,
                "final_qty": qty_now if qty_now is not None else 0,
                "target_qty": target_qty,
                "steps": steps_taken,
                "badge": badge,
                "error": "cliques não alteraram carrinho/qty (login? seletor?)",
            }

        if qty_now is not None and 0 < qty_now < target_qty and not badge_grew:
            print(
                f"[browser] set_qty PARCIAL index={index} qty={qty_now}/{target_qty}",
                flush=True,
            )
            return {
                "success": False,
                "final_qty": qty_now,
                "target_qty": target_qty,
                "steps": steps_taken,
                "badge": badge,
                "error": f"qty ficou em {qty_now}, alvo {target_qty}",
            }

        print(
            f"[browser] set_qty done index={index} clicks={steps_taken} "
            f"target={target_qty} qty={qty_now} badge={badge}",
            flush=True,
        )
        return {
            "success": True,
            "final_qty": qty_now if qty_now is not None else target_qty,
            "target_qty": target_qty,
            "steps": steps_taken,
            "badge": badge,
            "error": None,
        }

    def session_warning(self) -> Optional[str]:
        """Detecta CTA de login visível — carrinho autenticado provavelmente indisponível."""
        try:
            body = (self.page.inner_text("body") or "")[:4000].lower()
        except Exception:
            return None
        markers = (
            "entre ou cadastre-se",
            "fazer login",
            "identifique-se",
            "entrar / cadastrar",
            "entrar ou cadastrar",
        )
        if any(m in body for m in markers):
            return (
                "página sugere que não há sessão logada — "
                "adicionar ao carrinho pode falhar"
            )
        return None


    def _await_qty_mutation(
        self, index: int, previous_qty: int, max_attempts: int
    ) -> None:
        """Polling: aguarda current_qty mudar (até timeout). Resolve debounce."""
        deadline = time.time() + (self.click_settle_ms / 1000.0) * max_attempts
        while time.time() < deadline:
            if self.current_qty(index) != previous_qty:
                return
            time.sleep(self.click_settle_ms / 1000.0 / 3)
        # timeout silencioso — set_qty fará nova iteração ou abortará

    def open_cart(self) -> None:
        self.page.evaluate("() => andorinha_open_cart()")


# =============================================================================
# Smoke test CLI
# =============================================================================


def _login_session() -> int:
    """Abre o Chromium persistente na home do Andorinha só para o usuário logar."""
    profile = str(Path(tempfile.gettempdir()) / "andorinha-pw-profile")
    print(f"[login] abrindo Chromium do Playwright", flush=True)
    print(f"[login] perfil: {profile}", flush=True)
    print("[login] faça login no Andorinha nesta janela (não no Chrome normal).", flush=True)
    with Browser(launch_own=True, keep_open=True, user_data_dir=profile) as b:
        b.goto_home()
        warn = b.session_warning()
        if warn:
            print(f"[login] {warn}", flush=True)
        else:
            print("[login] nenhum CTA de login visível — sessão pode já estar ativa.", flush=True)
    print("[login] perfil salvo. Pode rodar o orchestrator sem --dry-run.", flush=True)
    return 0


def _smoke_test() -> int:
    """python -m orchestrator.browser [--login] [--cdp] [--click] <query>
    --login: só abre o site para você entrar (mesmo perfil das compras).
    Default: lanca Chromium proprio. --cdp usa localhost:9222.
    --click tenta set_qty(0,1) no primeiro resultado.
    """
    import sys
    args = sys.argv[1:]
    if "--login" in args:
        return _login_session()
    use_cdp = "--cdp" in args
    do_click = "--click" in args
    args = [a for a in args if a not in ("--cdp", "--click", "--launch-own")]
    query = args[0] if args else "papel higienico"
    with Browser(launch_own=not use_cdp) as b:
        b.search(query)
        results = b.get_results()
        print(f"# {len(results)} resultados para '{query}'")
        for r in results[:5]:
            print(
                f"[{r.index}] {r.name[:55]:<55} "
                f"R${r.price_num:6.2f} "
                f"| add={r.has_add} plus={r.has_plus} minus={r.has_minus} "
                f"| inCart={r.in_cart} qty={r.current_qty}"
            )
        if do_click and results:
            print("[smoke] tentando set_qty index=0 target=1 ...")
            print(b.set_qty(results[0].index, 1))
            print(f"[smoke] badge carrinho = {b.cart_badge()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_smoke_test())
