"""
report.py — Renderiza relatório markdown final da sessão de compras.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class ItemReport:
    raw: str
    status: str           # "ok" | "ambiguous_resolved" | "ambiguous_unresolved" | "not_found" | "failed_to_add"
    chosen_name: str | None
    chosen_index: int | None
    rule: str | None
    qty_target: int | float
    packs_added: int
    unit_price: float
    total_cost: float
    notes: list[str]


def render_report(
    list_path: str,
    profile_path: str,
    items: list[ItemReport],
    cart_badge_before: int | None,
    cart_badge_after: int | None,
    elapsed_s: float,
) -> str:
    now = datetime.now(timezone.utc).astimezone()
    total_cost = sum(i.total_cost for i in items if i.total_cost)
    by_status: dict[str, list[ItemReport]] = {}
    for i in items:
        by_status.setdefault(i.status, []).append(i)

    lines: list[str] = []
    lines.append("# 🛒 Andorinha — Relatório de Compras")
    lines.append("")
    lines.append(f"- **Data**: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    lines.append(f"- **Lista**: `{list_path}`")
    lines.append(f"- **Perfil**: `{profile_path}`")
    lines.append(f"- **Tempo de execução**: {elapsed_s:.1f}s")
    lines.append(f"- **Itens processados**: {len(items)}")
    lines.append(f"- **Custo estimado total**: R$ {total_cost:.2f}")
    if cart_badge_before is not None and cart_badge_after is not None:
        delta = cart_badge_after - cart_badge_before
        lines.append(f"- **Carrinho**: {cart_badge_before} → {cart_badge_after} unidades (+{delta})")
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
        lines.append(f"## ⚙️ Ambiguidades auto-resolvidas (sem LLM) ({len(amb_auto)})")
        lines.append("")
        lines.extend(_table(amb_auto))
        for it in amb_auto:
            if it.notes:
                lines.append(f"  - `{it.raw[:30]}`: " + " · ".join(it.notes))
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
