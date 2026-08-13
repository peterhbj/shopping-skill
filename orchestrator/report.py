"""
report.py — Renderiza relatório markdown final da sessão de compras.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .enricher import normalize


_STATUS_ALIAS = {
    "decided": "ok",
    "ok": "ok",
    "ambiguous_resolved": "ambiguous_resolved",
    "ambiguous_auto": "ambiguous_auto",
    "ambiguous_unresolved": "ambiguous_unresolved",
    "needs_grok": "ambiguous_unresolved",
    "not_found": "not_found",
    "failed_to_add": "failed_to_add",
}


@dataclass
class ItemReport:
    raw: str
    status: str
    chosen_name: str | None
    chosen_index: int | None
    rule: str | None
    qty_target: int | float
    packs_added: int
    unit_price: float
    total_cost: float
    notes: list[str]
    unit: str | None = None
    cart_qty: int | None = None


def normalize_status(status: str) -> str:
    return _STATUS_ALIAS.get(status, status)


def _is_weight_unit(unit: str | None) -> bool:
    return (unit or "").lower().strip() in ("kg", "g", "grama", "gramas")


def _names_match(chosen: str | None, cart_name: str) -> bool:
    if not chosen:
        return False
    a, b = normalize(chosen), normalize(cart_name)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    wa = {t for t in a.split() if len(t) >= 4}
    wb = set(b.split())
    if not wa:
        return False
    return len(wa & wb) >= min(2, len(wa))


def reconcile_with_cart(items: list[ItemReport], lines: list[dict]) -> tuple[list[ItemReport], int]:
    """
    Fonte da verdade: linhas do drawer do carrinho.
    kg: presença basta. unidade: qty da linha >= packs_added.
    Se o scrape voltar vazio, não marca tudo como missing (seletor pode ter falhado).
    """
    if not lines:
        for it in items:
            it.status = normalize_status(it.status)
        return items, 0

    used: set[int] = set()
    matched = 0
    for it in items:
        it.status = normalize_status(it.status)
        if it.status == "not_found" or not it.chosen_name:
            continue
        hit_i = None
        for i, line in enumerate(lines):
            if i in used:
                continue
            if _names_match(it.chosen_name, line.get("name") or ""):
                hit_i = i
                break
        if hit_i is None:
            if it.status != "not_found":
                it.status = "failed_to_add"
                it.notes = list(it.notes) + ["não achei esta linha no carrinho"]
            continue
        used.add(hit_i)
        line_qty = int(lines[hit_i].get("qty") or 1)
        it.cart_qty = line_qty
        matched += 1
        if _is_weight_unit(it.unit):
            continue
        need = int(it.packs_added or it.qty_target or 1)
        if line_qty < need:
            it.status = "failed_to_add"
            it.notes = list(it.notes) + [f"no carrinho qty={line_qty}, alvo={need}"]
    return items, matched


def render_report(
    list_path: str,
    profile_path: str,
    items: list[ItemReport],
    cart_badge_before: int | None,
    cart_badge_after: int | None,
    elapsed_s: float,
    cart_lines_count: int | None = None,
    cart_matched: int | None = None,
) -> str:
    now = datetime.now(timezone.utc).astimezone()
    total_cost = sum(i.total_cost for i in items if i.total_cost)
    by_status: dict[str, list[ItemReport]] = {}
    for i in items:
        by_status.setdefault(normalize_status(i.status), []).append(i)

    lines: list[str] = []
    lines.append("# Andorinha — Relatório de Compras")
    lines.append("")
    lines.append(f"- **Data**: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    lines.append(f"- **Lista**: `{list_path}`")
    lines.append(f"- **Perfil**: `{profile_path}`")
    lines.append(f"- **Tempo de execução**: {elapsed_s:.1f}s")
    lines.append(f"- **Itens processados**: {len(items)}")
    lines.append(f"- **Custo estimado total**: R$ {total_cost:.2f}")
    if cart_lines_count is not None:
        matched = cart_matched if cart_matched is not None else "?"
        lines.append(f"- **Carrinho (linhas lidas)**: {cart_lines_count} · bateram com a lista: {matched}")
    elif cart_badge_before is not None and cart_badge_after is not None:
        delta = cart_badge_after - cart_badge_before
        lines.append(f"- **Badge (pode mentir)**: {cart_badge_before} → {cart_badge_after} (+{delta})")
    lines.append("")

    def _table(group: list[ItemReport]) -> list[str]:
        rows = ["| # | Item | Escolha | Qty | R$ Unit | R$ Total | Regra |",
                "|---|------|---------|-----|---------|----------|-------|"]
        for n, it in enumerate(group, 1):
            name = (it.chosen_name or "—")[:50]
            rows.append(
                f"| {n} | {it.raw[:30]} | {name} | {it.packs_added}×{it.qty_target} | "
                f"R${it.unit_price:.2f} | R${it.total_cost:.2f} | `{it.rule or '-'}` |"
            )
        return rows

    ok = by_status.get("ok", [])
    amb_res = by_status.get("ambiguous_resolved", [])
    amb_auto = by_status.get("ambiguous_auto", [])
    amb_unres = by_status.get("ambiguous_unresolved", [])
    not_found = by_status.get("not_found", [])
    failed = by_status.get("failed_to_add", [])

    if ok:
        lines.append(f"## ✅ Adicionados via regras determinísticas ({len(ok)})")
        lines.append("")
        lines.extend(_table(ok))
        lines.append("")
    if amb_res:
        lines.append(f"## 🤖 Resolvidos por LLM ({len(amb_res)})")
        lines.append("")
        lines.extend(_table(amb_res))
        for it in amb_res:
            if it.notes:
                lines.append(f"  - `{it.raw[:30]}`: " + " · ".join(it.notes))
        lines.append("")
    if amb_auto:
        lines.append(f"## Ambiguidades auto-resolvidas (sem Grok) ({len(amb_auto)})")
        lines.append("")
        lines.extend(_table(amb_auto))
        grok_err = [n for it in amb_auto for n in (it.notes or []) if "LLM falhou" in n or "grok CLI" in n]
        if grok_err:
            lines.append(f"  - Grok CLI falhou em {len(amb_auto)} item(ns). Primeiro erro: {grok_err[0][:240]}")
        lines.append("")
    if amb_unres:
        lines.append(f"## ⚠️ Ambíguos sem resolução ({len(amb_unres)})")
        lines.append("")
        for it in amb_unres:
            lines.append(f"- **{it.raw}**: " + " · ".join(it.notes or ["sem detalhes"]))
        lines.append("")
    if not_found:
        lines.append(f"## ❌ Não encontrados ({len(not_found)})")
        lines.append("")
        for it in not_found:
            lines.append(f"- **{it.raw}**: " + " · ".join(it.notes or ["—"]))
        lines.append("")
    if failed:
        lines.append(f"## 💥 Falha ao adicionar ({len(failed)})")
        lines.append("")
        for it in failed:
            lines.append(f"- **{it.raw}** (escolha: {it.chosen_name}): " + " · ".join(it.notes))
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## ℹ️ Próximos passos")
    lines.append("")
    lines.append("- Revise os itens em ⚠️ e ❌ antes de finalizar o pedido.")
    lines.append("- Carrinho está montado mas não foi finalizado (checkout manual).")
    return "\n".join(lines) + "\n"
