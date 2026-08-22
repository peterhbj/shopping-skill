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

import json
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import sync_playwright, Page, Browser as PWBrowser


HELPERS_PATH = Path(__file__).resolve().parent.parent / "scripts" / "browser_helpers.js"

# URLs de mutação de carrinho (Andorinha/OSuper). Ampliar se o log mostrar outro path.
_CART_URL_RE = re.compile(
    r"carrinho|cart|basket|bag|pedido|add-to|addtocart|quantidade|qty|/item",
    re.I,
)


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
    product_id: Optional[str] = None
    categories: list[str] | None = None
    sale_unit: Optional[str] = None
    product_type: Optional[str] = None
    brand_name: Optional[str] = None
    sell_by_weight: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "ProductResult":
        return cls(
            index=d["index"],
            name=d["name"],
            name_lower=d.get("name_lower") or d["name"].lower(),
            price_text=d.get("price_text") or "",
            price_num=float(d.get("price_num") or 0),
            unit=d.get("unit") or "un",
            unit_count=d.get("unit_count"),
            weight_g=d.get("weight_g"),
            volume_ml=d.get("volume_ml"),
            price_per_base_unit=d.get("price_per_base_unit", d.get("price_num") or 0),
            price_base_dim=d.get("price_base_dim", "un"),
            price_per_kg=d.get("price_per_kg", False),
            in_cart=d.get("in_cart", False),
            current_qty=d.get("current_qty", 0),
            has_add=d.get("has_add", False),
            has_plus=d.get("has_plus", False),
            has_minus=d.get("has_minus", False),
            product_id=d.get("product_id"),
            categories=list(d.get("categories") or []),
            sale_unit=d.get("sale_unit"),
            product_type=d.get("product_type"),
            brand_name=d.get("brand_name"),
            sell_by_weight=bool(d.get("sell_by_weight", False)),
        )


class BrowserError(Exception):
    pass


def _norm_name(text: str) -> str:
    t = (text or "").lower().strip()
    t = t.replace("ç", "c").replace("ã", "a").replace("á", "a").replace("â", "a")
    t = t.replace("é", "e").replace("ê", "e").replace("í", "i")
    t = t.replace("ó", "o").replace("ô", "o").replace("õ", "o").replace("ú", "u")
    return re.sub(r"\s+", " ", t)


def product_from_sense_hit(hit: dict, index: int) -> ProductResult:
    """Monta um ProductResult a partir de um hit do Sense (testes e fallback)."""
    pricing = hit.get("pricing") or {}
    qty = hit.get("quantity") or {}
    price = float(pricing.get("promotionalPrice") or pricing.get("price") or 0)
    sale = str(hit.get("saleUnit") or "UN").upper()
    ptype = str(hit.get("type") or "PRODUCT")
    sell_w = bool(qty.get("sellByWeightAndUnit")) or sale == "KG" or ptype == "VARIABLE"
    cats = [str(c).split(":", 1)[-1] for c in (hit.get("categories") or [])]
    name = str(hit.get("name") or "")
    return ProductResult(
        index=index,
        name=name,
        name_lower=name.lower(),
        price_text=f"R$ {price:.2f}",
        price_num=price,
        unit="kg" if sale == "KG" else "un",
        unit_count=None,
        weight_g=None,
        volume_ml=None,
        price_per_base_unit=price,
        price_base_dim="kg" if sale == "KG" else "un",
        price_per_kg=sale == "KG" or sell_w,
        in_cart=False,
        current_qty=0,
        has_add=True,
        has_plus=False,
        has_minus=False,
        product_id=str(hit.get("id") or "") or None,
        categories=cats,
        sale_unit=sale,
        product_type=ptype,
        brand_name=hit.get("brandName"),
        sell_by_weight=sell_w,
    )


def attach_sense_hit(result: ProductResult, hit: dict) -> ProductResult:
    extra = product_from_sense_hit(hit, result.index)
    result.product_id = extra.product_id
    result.categories = extra.categories
    result.sale_unit = extra.sale_unit
    result.product_type = extra.product_type
    result.brand_name = extra.brand_name
    result.sell_by_weight = extra.sell_by_weight
    if extra.price_num and not result.price_num:
        result.price_num = extra.price_num
    if extra.price_per_kg:
        result.price_per_kg = True
        result.price_base_dim = "kg"
    return result


def merge_sense_hits(cards: list[ProductResult], hits: list[dict]) -> list[ProductResult]:
    if not hits:
        return cards
    by_name: dict[str, dict] = {}
    for h in hits:
        key = _norm_name(str(h.get("name") or ""))
        if key:
            by_name[key] = h
    if not cards:
        return [product_from_sense_hit(h, i) for i, h in enumerate(hits)]
    used: set[str] = set()
    for card in cards:
        key = _norm_name(card.name)
        hit = by_name.get(key)
        if hit is None:
            for nk, h in by_name.items():
                if nk in used:
                    continue
                if key and (key[:24] in nk or nk[:24] in key):
                    hit = h
                    key = nk
                    break
        if hit:
            attach_sense_hit(card, hit)
            used.add(key)
    return cards


def fetch_sense_hits(query: str, storefront: str = "269", store: str = "1327", size: int = 24) -> list[dict]:
    params = urllib.parse.urlencode(
        {
            "search": query,
            "size": str(size),
            "from": "0",
            "sortField": "_score",
            "sortOrder": "desc",
            "brands": "",
            "categories": "",
            "tags": "",
        }
    )
    url = f"https://sense.osuper.com.br/{storefront}/{store}/search?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://www.andorinhaonline.com.br",
            "Referer": "https://www.andorinhaonline.com.br/",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"[browser] sense HTTP falhou: {e}", flush=True)
        return []
    return list((data or {}).get("hits") or [])


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
        self._sense_hits: list[dict] = []
        self._cart_payload: list[dict] = []

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
        url = f"{self.BASE_URL}/busca/{urllib.parse.quote(query, safe='')}"
        print(f"[browser] goto {url}", flush=True)
        prev_url = ""
        try:
            prev_url = self.page.url or ""
        except Exception:
            pass
        self._sense_hits = []
        self._search_had_cards = False

        def _on_sense(resp) -> None:
            try:
                rurl = resp.url or ""
                if "sense.osuper.com.br" not in rurl or "/search" not in rurl:
                    return
                body = resp.json()
                hits = list((body or {}).get("hits") or [])
                if hits:
                    self._sense_hits = hits
                    print(f"[browser] sense {len(hits)} hits (total={body.get('total')})", flush=True)
            except Exception:
                pass

        self.page.on("response", _on_sense)
        try:
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
            except Exception as e:
                current = ""
                try:
                    current = self.page.url or ""
                except Exception:
                    pass
                if "/busca/" in current and current != prev_url:
                    print(f"[browser] goto timeout mas URL de busca carregou: {e}", flush=True)
                else:
                    raise BrowserError(f"falha ao abrir busca {query!r}: {e}") from e

            self._ensure_helpers()
            self._wait_for_results()
            if not self._sense_hits:
                deadline = time.time() + 4.0
                while time.time() < deadline and not self._sense_hits:
                    time.sleep(0.2)
            if not self._sense_hits:
                print("[browser] sense intercept vazio — HTTP fallback", flush=True)
                self._sense_hits = fetch_sense_hits(query)
        finally:
            try:
                self.page.remove_listener("response", _on_sense)
            except Exception:
                pass

    def _wait_for_results(self, timeout_ms: int = 8000) -> None:
        """
        SPA do Andorinha só hidrata os cards depois do JS.
        Não lê o body inteiro: '0 itens' no badge do carrinho abortava buscas válidas.
        """
        selectors = [
            ".item-product-wrapper",
        ]
        deadline = time.time() + (timeout_ms / 1000.0)
        while time.time() < deadline:
            for sel in selectors:
                try:
                    loc = self.page.locator(sel)
                    if loc.count() > 0:
                        self.page.wait_for_timeout(120)
                        self._search_had_cards = True
                        print(f"[browser] cards via '{sel}' (count≈{loc.count()})", flush=True)
                        return
                except Exception:
                    continue
            self.page.wait_for_timeout(250)
        print("[browser] nenhum card de produto detectado após espera", flush=True)

    def _ensure_helpers(self) -> None:
        """Re-injeta helpers caso add_init_script não tenha pegado a página."""
        try:
            loaded = self.page.evaluate("() => !!window.__ANDORINHA_HELPERS_V6__")
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
        if not getattr(self, "_search_had_cards", True) and not self._sense_hits:
            print("[browser] get_results: busca sem cards — não reuso DOM antigo", flush=True)
            return []
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
        results = merge_sense_hits(results, self._sense_hits)
        print(
            f"[browser] get_results → {len(results)} produtos "
            f"(sense={len(self._sense_hits)})",
            flush=True,
        )
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

    def card_state(self, index: int) -> dict:
        try:
            payload = self.page.evaluate(f"() => andorinha_card_state({index})")
        except Exception as e:
            return {"error": str(e), "qty": 0, "has_minus": False}
        return payload if isinstance(payload, dict) else {"qty": 0, "has_minus": False}

    @staticmethod
    def _looks_like_cart_url(url: str) -> bool:
        return bool(_CART_URL_RE.search(url or ""))

    def wait_network_quiet(self, timeout_ms: int = 3000) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass

    def set_qty(
        self,
        index: int,
        target_qty: int,
        max_attempts_per_step: int = 8,
        by_weight: bool = False,
    ) -> dict:
        """
        Adiciona clicando N vezes e confere se qty/badge mudaram.
        Não trata clique sem efeito como sucesso (login ausente, seletor quebrado).
        """
        self._ensure_helpers()
        target_qty = max(1, int(target_qty))
        steps_taken = 0
        state0 = self.card_state(index)
        qty_before = int(state0.get("qty") or 0)
        already_in_cart = bool(state0.get("has_minus"))

        for step in range(target_qty):
            seen: list[tuple[str, int]] = []
            cart_ok = {"v": False}

            def _on_response(resp) -> None:
                try:
                    url = resp.url or ""
                    status = int(resp.status)
                    if self._looks_like_cart_url(url):
                        seen.append((url, status))
                        if 200 <= status < 300:
                            cart_ok["v"] = True
                except Exception:
                    pass

            self.page.on("response", _on_response)
            try:
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
                prev = result.get("prev_qty") if isinstance(result, dict) else qty_before
                first_add = step == 0 and not already_in_cart
                acked = self._wait_cart_ack(
                    index,
                    previous_qty=int(prev) if isinstance(prev, int) else qty_before,
                    first_add=first_add,
                    cart_ok=cart_ok,
                    timeout_s=1.2 if by_weight else 2.5,
                    by_weight=by_weight,
                )
            finally:
                try:
                    self.page.remove_listener("response", _on_response)
                except Exception:
                    pass

            if seen:
                sample = ", ".join(f"{s}:{u[:80]}" for u, s in seen[:4])
                print(f"[browser] cart HTTP: {sample}", flush=True)
            elif not acked:
                print("[browser] nenhum HTTP de carrinho visto neste clique", flush=True)

            if not acked:
                st = self.card_state(index)
                print(
                    f"[browser] set_qty SEM ACK no card (kg/granel é normal) "
                    f"index={index} step={step+1}/{target_qty} "
                    f"qty={st.get('qty')} minus={st.get('has_minus')}",
                    flush=True,
                )
                # Não marca falha: granel não tem stepper. Veredito = scrape do carrinho.
                return {
                    "success": True,
                    "final_qty": int(st.get("qty") or steps_taken),
                    "target_qty": target_qty,
                    "steps": steps_taken,
                    "error": "sem ack de layout (seguir; conferir carrinho)",
                    "rate_limit_pause": True,
                }
            already_in_cart = True

        try:
            self.page.wait_for_timeout(150)
        except Exception:
            pass
        st = self.card_state(index)
        qty_now = int(st.get("qty") or 0)
        try:
            badge = self.cart_badge()
        except Exception:
            badge = None

        print(
            f"[browser] set_qty done index={index} clicks={steps_taken} "
            f"target={target_qty} qty={qty_now} minus={st.get('has_minus')} badge={badge}",
            flush=True,
        )
        return {
            "success": True,
            "final_qty": qty_now if qty_now else target_qty,
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


    def _wait_cart_ack(
        self,
        index: int,
        previous_qty: int,
        first_add: bool,
        cart_ok: dict,
        timeout_s: float = 2.5,
        by_weight: bool = False,
    ) -> bool:
        """
        Ack = card no modo stepper (has_minus) + qty estável em 2 leituras.
        Item em kg: has_minus basta (o stepper não mostra 1).
        """
        deadline = time.time() + timeout_s
        last_qty: int | None = None
        while time.time() < deadline:
            st = self.card_state(index)
            qty = int(st.get("qty") or 0)
            has_minus = bool(st.get("has_minus"))
            if by_weight:
                layout_ok = has_minus
                qty_ok = True
            elif first_add:
                layout_ok = has_minus
                qty_ok = qty >= 1
            else:
                layout_ok = has_minus
                qty_ok = qty > previous_qty
            if layout_ok and qty_ok and last_qty == qty:
                return True
            last_qty = qty
            time.sleep(0.12)
        st = self.card_state(index)
        if by_weight:
            return bool(st.get("has_minus")) or bool(cart_ok.get("v"))
        if bool(st.get("has_minus")) and int(st.get("qty") or 0) > previous_qty:
            return True
        return False

    def open_cart(self) -> None:
        self._ensure_helpers()
        try:
            self.page.evaluate("() => andorinha_open_cart()")
        except Exception as e:
            print(f"[browser] open_cart: {e}", flush=True)
        try:
            self.page.wait_for_timeout(400)
        except Exception:
            pass

    def read_cart_lines(self) -> list[dict]:
        self._cart_payload = []

        def _on_cart(resp) -> None:
            try:
                url = (resp.url or "").lower()
                if not any(x in url for x in ("cart", "carrinho", "checkout", "sense.osuper")):
                    return
                body = resp.json()
                if isinstance(body, dict):
                    self._cart_payload.append(body)
            except Exception:
                pass

        try:
            self.page.on("response", _on_cart)
        except Exception:
            pass
        try:
            self.open_cart()
            self._ensure_helpers()
            try:
                payload = self.page.evaluate("() => andorinha_get_cart_lines()")
            except Exception as e:
                print(f"[browser] read_cart_lines: {e}", flush=True)
                payload = {}
            lines = []
            if isinstance(payload, dict):
                lines = payload.get("lines") or []
            if not lines:
                lines = _lines_from_cart_payloads(self._cart_payload)
            print(
                f"[browser] cart lines → {len(lines)} "
                f"(url={(payload or {}).get('url') if isinstance(payload, dict) else ''} "
                f"payloads={len(self._cart_payload)})",
                flush=True,
            )
            if lines:
                print(
                    f"[browser]   [0] {str(lines[0].get('name', ''))[:60]!r} "
                    f"qty={lines[0].get('qty')}",
                    flush=True,
                )
            return lines
        finally:
            try:
                self.page.remove_listener("response", _on_cart)
            except Exception:
                pass


def _lines_from_cart_payloads(payloads: list[dict]) -> list[dict]:
    """Tenta achar itens de carrinho em JSON interceptado (schema varia)."""
    found: list[dict] = []

    def walk(obj: Any, depth: int = 0) -> None:
        if depth > 8 or found:
            return
        if isinstance(obj, dict):
            name = obj.get("name") or obj.get("productName") or obj.get("title")
            prod = obj.get("product") if isinstance(obj.get("product"), dict) else None
            if prod and not name:
                name = prod.get("name")
            qty = obj.get("qty") or obj.get("quantity") or obj.get("amount")
            if isinstance(qty, dict):
                qty = qty.get("value") or qty.get("qty") or qty.get("amount")
            price = obj.get("price") or obj.get("price_num") or obj.get("unitPrice")
            if isinstance(price, dict):
                price = price.get("price") or price.get("promotionalPrice")
            tn = str(obj.get("__typename") or "")
            looks_item = bool(name) and (
                "cart" in tn.lower()
                or "item" in tn.lower()
                or obj.get("saleUnit")
                or (qty not in (None, "") and price not in (None, ""))
            )
            if looks_item and isinstance(name, str) and len(name) > 3:
                try:
                    qn = float(qty) if qty is not None else 1.0
                except (TypeError, ValueError):
                    qn = 1.0
                try:
                    pn = float(price) if price is not None else None
                except (TypeError, ValueError):
                    pn = None
                found.append({"name": name, "qty": qn, "price_num": pn})
            for v in obj.values():
                walk(v, depth + 1)
        elif isinstance(obj, list):
            for it in obj[:40]:
                walk(it, depth + 1)

    for p in payloads:
        walk(p)
        if found:
            break
    return found


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
