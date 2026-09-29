"""Read completed orders from an authenticated Andorinha browser session.

The storefront has no documented order export. This adapter only accepts
complete order objects observed while navigating the account's visible order
pages; it never guesses an account API URL or calls checkout endpoints.
"""

from __future__ import annotations

from typing import Any

from .browser import Browser
from .history import PurchaseHistory


def _orders_in(payload: Any, depth: int = 0) -> list[dict]:
    if depth > 8:
        return []
    found: list[dict] = []
    if isinstance(payload, list):
        for part in payload[:200]:
            found.extend(_orders_in(part, depth + 1))
    elif isinstance(payload, dict):
        status = payload.get("status") or payload.get("situation")
        items = payload.get("items") or payload.get("orderItems") or payload.get("products")
        oid = payload.get("orderId") or payload.get("order_id") or payload.get("id")
        date = payload.get("createdAt") or payload.get("date") or payload.get("orderedAt")
        if oid and date and status and isinstance(items, list) and items:
            normalized = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                product = item.get("product") if isinstance(item.get("product"), dict) else item
                normalized.append({
                    "product_id": product.get("id") or product.get("productId") or product.get("sku"),
                    "name": product.get("name") or product.get("description"),
                    "brand": product.get("brandName") or product.get("brand"),
                    "quantity": item.get("quantity") or item.get("qty") or 1,
                    "unit": product.get("saleUnit") or item.get("unit"),
                    "unit_price": item.get("unitPrice") or item.get("price"),
                })
            found.append({"order_id": str(oid), "ordered_at": str(date),
                          "status": str(status), "items": normalized})
        else:
            for value in payload.values():
                if isinstance(value, (list, dict)):
                    found.extend(_orders_in(value, depth + 1))
    return found


def sync_orders(history: PurchaseHistory) -> dict:
    observed: list[dict] = []
    with Browser(launch_own=True, keep_open=False) as browser:
        page = browser.page
        def capture(response):
            try:
                if "andorinhaonline.com.br" not in response.url and "osuper.com.br" not in response.url:
                    return
                if "pedido" not in response.url.lower() and "order" not in response.url.lower():
                    return
                if "json" not in (response.headers.get("content-type") or ""):
                    return
                observed.extend(_orders_in(response.json()))
            except Exception:
                pass
        page.on("response", capture)
        browser.goto_home()
        if browser.session_warning():
            return {"state": "login_required", "message": "Entre no Andorinha pelo navegador do aplicativo antes de sincronizar."}
        profile = page.locator(".profile-btn")
        if profile.count():
            profile.first.click()
        order_link = page.get_by_text("Meus pedidos", exact=False)
        if not order_link.count():
            return {"state": "unavailable", "message": "Não encontrei 'Meus pedidos' nesta sessão. Entre no Andorinha ou importe JSON/CSV; histórico incompleto."}
        order_link.first.click()
        page.wait_for_load_state("domcontentloaded")
        # Visit only order links visible in the authenticated account UI.
        links = page.locator("a[href]").evaluate_all(
            "els => els.filter(e => /pedido|order/i.test((e.textContent || '') + ' ' + e.getAttribute('href'))).map(e => e.href).slice(0, 200)"
        )
        for href in dict.fromkeys(links):
            if not href.startswith("https://www.andorinhaonline.com.br/"):
                continue
            try:
                page.goto(href, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                continue
        page.remove_listener("response", capture)
    unique = {order["order_id"]: order for order in observed}
    if not unique:
        return {"state": "unavailable", "message": "Nenhum detalhe de pedido concluído foi exposto pela página. Importe um arquivo JSON ou CSV."}
    result = history.import_orders(list(unique.values()), source="andorinha-account")
    return {"state": "ok", **result}
