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
    "needs_grok": "needs_user",
    "needs_user": "needs_user",
    "not_found": "needs_user",
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
    cart_qty: float | None = None
    cart_line_total: float | None = None


def normalize_status(status: str) -> str:
    return _STATUS_ALIAS.get(status, status)


def _is_weight_unit(unit: str | None) -> bool:
    return (unit or "").lower().strip() in ("kg", "g", "grama", "gramas")


_NAME_STOP = frozenset(
    "com sem para tipo longa vida embalagem pacote caixa und kg".split()
)


def _sig_tokens(text: str) -> set[str]:
    return {t for t in normalize(text).split() if len(t) >= 4 and t not in _NAME_STOP}


def _names_match(chosen: str | None, cart_name: str) -> bool:
    if not chosen:
        return False
    a, b = normalize(chosen), normalize(cart_name)
    if not a or not b:
        return False
    if a == b or a[:48] in b or b[:48] in a:
        return True
    wa, wb = _sig_tokens(chosen), _sig_tokens(cart_name)
    if not wa or not wb:
        return False
    return len(wa & wb) >= min(3, len(wa))


def estimated_search_total(items: list[ItemReport]) -> float:
    """Soma packs × preço do card. Em kg isso é ~1 clique (R$/kg), não o peso real."""
    return round(sum(i.total_cost for i in items if i.total_cost), 2)


def cart_scrape_total(items: list[ItemReport]) -> float | None:
    vals = [i.cart_line_total for i in items if i.cart_line_total is not None]
    if not vals:
        return None
    return round(sum(vals), 2)


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
        line = lines[hit_i]
        line_qty = float(line.get("qty") or 1)
        it.cart_qty = line_qty
        unit_p = line.get("price_num")
        line_total = line.get("line_total")
        if line_total is not None:
            it.cart_line_total = float(line_total)
        elif unit_p is not None:
            unit_p = float(unit_p)
            # Drawer às vezes mostra só o total da linha (R$101,88), não o unitário.
            expected = (it.unit_price or 0) * line_qty
            if line_qty > 1 and expected > 0 and unit_p >= expected * 0.8:
                it.cart_line_total = round(unit_p, 2)
            else:
                it.cart_line_total = round(unit_p * line_qty, 2)
        matched += 1
        if _is_weight_unit(it.unit):
            continue
        need = float(it.packs_added or it.qty_target or 1)
        if line_qty + 1e-6 < need:
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
    estimate = estimated_search_total(items)
    scraped = cart_scrape_total(items)
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
    if scraped is not None:
        lines.append(f"- **Total no carrinho (linhas lidas)**: R$ {scraped:.2f}")
        lines.append(
            f"- **Estimativa pelos cards da busca**: R$ {estimate:.2f} "
            f"(packs × preço do card; em kg é 1 clique / R$ por kg, **não** o peso real)"
        )
    else:
        lines.append(
            f"- **Estimativa pelos cards da busca**: R$ {estimate:.2f} "
            f"— **não é o total do site**. Carnes/hortifruti entram com 1 clique "
            f"(peso na balança), então este número costuma ficar mais alto que o carrinho."
        )
    if cart_lines_count is not None:
        matched = cart_matched if cart_matched is not None else "?"
        lines.append(f"- **Carrinho (linhas lidas)**: {cart_lines_count} · bateram com a lista: {matched}")
        if cart_lines_count == 0:
            lines.append(
                "- **Carrinho não lido** — o drawer voltou vazio. "
                "Isso **não** prova que os itens entraram; conferir no site."
            )
    elif cart_badge_before is not None and cart_badge_after is not None:
        delta = cart_badge_after - cart_badge_before
        lines.append(f"- **Badge (pode mentir)**: {cart_badge_before} → {cart_badge_after} (+{delta})")
    lines.append("")

    def _table(group: list[ItemReport]) -> list[str]:
        rows = ["| # | Item | Escolha | Qty | R$ Unit | R$ Total | Regra |",
                "|---|------|---------|-----|---------|----------|-------|"]
        for n, it in enumerate(group, 1):
            name = (it.chosen_name or "—")[:50]
            if _is_weight_unit(it.unit):
                qty_s = f"1 clique (~{it.qty_target}{it.unit})"
                tot = it.cart_line_total if it.cart_line_total is not None else it.total_cost
                tot_s = f"R${tot:.2f}/kg" if it.cart_line_total is None else f"R${tot:.2f}"
            else:
                if float(it.packs_added or 0) == float(it.qty_target or 0):
                    qty_s = str(int(it.packs_added or it.qty_target or 0))
                else:
                    qty_s = f"{it.packs_added}×{it.qty_target}"
                tot = it.cart_line_total if it.cart_line_total is not None else it.total_cost
                tot_s = f"R${tot:.2f}"
            rows.append(
                f"| {n} | {it.raw[:30]} | {name} | {qty_s} | "
                f"R${it.unit_price:.2f} | {tot_s} | `{it.rule or '-'}` |"
            )
        return rows

    ok = by_status.get("ok", [])
    amb_res = by_status.get("ambiguous_resolved", [])
    amb_auto = by_status.get("ambiguous_auto", [])
    amb_unres = by_status.get("ambiguous_unresolved", [])
    needs_user = by_status.get("needs_user", [])
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
    if needs_user:
        lines.append(f"## ❓ Esperando você ({len(needs_user)})")
        lines.append("")
        lines.append("Não foram para o carrinho. Responda e rode `resolve`.")
        lines.append("")
        for it in needs_user:
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


def render_duvidas(planned_items: list[dict]) -> str:
    """Perguntas para o usuário: busca, o que apareceu, o que não decidiram."""
    doubts = [
        it for it in planned_items
        if (it.get("status") or "") in ("needs_user", "needs_grok", "not_found")
    ]
    lines = [
        "# Dúvidas da busca — não foram para o carrinho",
        "",
        "Responda cada item (marca, tamanho, pular…). Com as respostas:",
        "`python -m orchestrator.main resolve --list lista-compras.md --profile preferencias.yaml --answers respostas.yaml`",
        "",
    ]
    if not doubts:
        lines.append("Nenhuma dúvida. Pode `apply`.")
        lines.append("")
        return "\n".join(lines)
    for n, it in enumerate(doubts, 1):
        lines.append(f"## {n}. {it.get('raw') or '?'}")
        lines.append("")
        lines.append(f"- **Busca**: `{it.get('search_term') or '—'}`")
        lines.append(f"- **Qty**: {it.get('qty') or 1} {it.get('unit') or ''}".rstrip())
        lines.append(f"- **Por quê**: " + " · ".join(it.get("notes") or ["não decidido"]))
        cands = it.get("candidates") or []
        if cands:
            lines.append("- **O que apareceu**:")
            for c in cands[:8]:
                price = c.get("price_num")
                price_s = f"R${price:.2f}" if isinstance(price, (int, float)) else "?"
                lines.append(f"  - [{c.get('index')}] {c.get('name')} — {price_s}")
        else:
            lines.append("- **O que apareceu**: nada")
        if it.get("preferred_name"):
            lines.append(f"- **Marca do perfil (não clicada)**: {it['preferred_name']}")
        lines.append("")
    lines.append("Exemplo `respostas.yaml`:")
    lines.append("")
    lines.append("```yaml")
    lines.append("respostas:")
    sample = doubts[0].get("raw") or "Item"
    lines.append(f"  - item: {sample}")
    lines.append("    texto: pular")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)
