"""
main.py — Orquestrador end-to-end da Andorinha Shopping Skill (v2.1).

Mudanças vs v2 original:
- LLM é OPCIONAL (flag --use-llm). Default = zero chamada de modelo.
- Quando Ambiguity e LLM desligado: auto-resolve (preferred ou cheapest).
- --launch-own como default prático para Grok Build / ambientes sem CDP.

CLI:
    python3 -m orchestrator.main \\
        --list lista-compras.md \\
        --profile perfil-compras.yaml \\
        --report-out /tmp/relatorio.md \\
        [--dry-run] [--use-llm] [--launch-own]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

import yaml

from .browser import Browser, BrowserError, ProductResult
from .enricher import enrich_list
from .llm.adapter import (
    GrokCLIAdapter,
    LLMAdapter,
    LLMCallError,
    render_ambiguity_prompt,
)
from .report import ItemReport, render_report
from .selector import (
    Ambiguity,
    Decision,
    NoResult,
    SelectorConfig,
    select,
)


PROMPTS_DIR = Path(__file__).parent / "llm" / "prompts"


def _load_config(profile_path: Path) -> tuple[SelectorConfig, dict]:
    data = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    cfg = data.get("config", {}) or {}
    household = data.get("household", {}) or {}
    return SelectorConfig(
        dominance_threshold_pct=float(cfg.get("dominance_threshold_pct", 15.0)),
        pack_optimization=bool(cfg.get("pack_optimization", True)),
        max_packs_per_item=int(cfg.get("max_packs_per_item", 6)),
        household_lactose_free=bool(household.get("lactose_free", False)),
    ), cfg


def _auto_resolve_ambiguity(
    ambiguity: Ambiguity,
    enriched_item: dict,
    config: SelectorConfig,
) -> Decision:
    """
    Resolve Ambiguity sem LLM:
    1. Se tem preferred → usa ele
    2. Senão → mais barato por unidade base
    Nunca multiplica typical_qty em gramas.
    """
    from .selector import _packs_needed_for_item

    if ambiguity.preferred:
        chosen = ambiguity.preferred
        rule = "auto-preferred (no-llm)"
    else:
        candidates = ambiguity.candidates
        chosen = min(
            candidates,
            key=lambda c: c.price_per_base_unit if c.price_per_base_unit > 0 else c.price_num,
        )
        rule = "auto-cheapest (no-llm)"

    qty = enriched_item.get("qty") or 1
    packs, total = _packs_needed_for_item(
        enriched_item, chosen, qty, config.pack_optimization, config.max_packs_per_item
    )

    return Decision(
        index=chosen.index,
        name=chosen.name,
        price_num=chosen.price_num,
        rule=rule,
        packs_needed=packs,
        total_cost=total,
        notes=[f"Ambiguity auto-resolvida: {ambiguity.kind}"],
    )


def _resolve_ambiguity_llm(
    ambiguity: Ambiguity,
    enriched_item: dict,
    adapter: LLMAdapter,
) -> tuple[int | None, str]:
    prompt = render_ambiguity_prompt(
        PROMPTS_DIR / "resolve_ambiguity.md",
        ambiguity,
        item_text=enriched_item.get("item_text") or enriched_item.get("raw", ""),
        qty=int(enriched_item.get("qty") or 1),
    )
    try:
        reply = adapter.ask_json(prompt, schema_hint={"index": "int", "reason": "string"})
        idx = int(reply.get("index"))
        reason = str(reply.get("reason", "LLM resolveu"))[:200]
        return idx, reason
    except (LLMCallError, ValueError, TypeError) as e:
        return None, f"LLM falhou: {e}"


def _packs_for(chosen: ProductResult, enriched_item: dict, config: SelectorConfig) -> tuple[int, float]:
    from .selector import _packs_needed_for_item
    qty = enriched_item.get("qty") or 1
    return _packs_needed_for_item(
        enriched_item, chosen, qty, config.pack_optimization, config.max_packs_per_item
    )


def run(
    list_path: Path,
    profile_path: Path,
    report_out: Path,
    dry_run: bool = False,
    launch_own: bool = True,
    use_llm: bool = False,
    keep_open: bool = True,
) -> int:
    started = time.time()

    print(f"[main] Enriquecendo lista: {list_path.name}", flush=True)
    enriched = enrich_list(list_path, profile_path)
    items = enriched["items"]
    print(
        f"[main] {enriched['meta']['total_items']} itens "
        f"({enriched['meta']['kb_matched']} matched, "
        f"{enriched['meta']['unmatched']} unmatched)",
        flush=True,
    )

    config, raw_cfg = _load_config(profile_path)
    llm_model = str(raw_cfg.get("llm_model", "haiku"))
    llm_timeout = int(raw_cfg.get("llm_timeout_s", 120))
    browser_cfg = raw_cfg.get("browser", {}) or {}
    cdp_url = str(browser_cfg.get("cdp_url", "http://localhost:9222"))
    click_settle_ms = int(browser_cfg.get("click_settle_ms", 250))
    nav_timeout_ms = int(browser_cfg.get("nav_timeout_ms", 15000))

    adapter: LLMAdapter | None = None
    if use_llm:
        adapter = GrokCLIAdapter(model=llm_model, timeout_s=llm_timeout)
        print("[main] LLM habilitado (grok -p)", flush=True)
    else:
        print("[main] LLM desligado — ambiguidades serão auto-resolvidas", flush=True)

    item_reports: list[ItemReport] = []

    print(f"[main] Conectando browser (dry_run={dry_run}, launch_own={launch_own})", flush=True)
    try:
        browser_ctx = Browser(
            cdp_url=cdp_url,
            click_settle_ms=click_settle_ms,
            nav_timeout_ms=nav_timeout_ms,
            launch_own=launch_own,
            keep_open=bool(keep_open and not dry_run),
        )
    except BrowserError as e:
        print(f"[main] ERRO: {e}", file=sys.stderr)
        return 2

    with browser_ctx as b:
        try:
            b.goto_home()
        except Exception as e:
            print(f"[main] aviso goto_home: {e}", flush=True)
        warn = None
        try:
            warn = b.session_warning()
        except Exception:
            warn = None
        if warn:
            print(f"[main] AVISO: {warn}", flush=True)

        try:
            badge_before = b.cart_badge()
        except Exception:
            badge_before = None

        for n, item in enumerate(items, 1):
            search = item.get("search_term", "").strip()
            qty = item.get("qty", 1)
            raw = item.get("raw", "")
            print(f"[main] [{n:2d}/{len(items)}] {raw[:35]:<35} → {search[:35]} (qty={qty})", flush=True)

            if not search:
                item_reports.append(_blank_report(raw, "not_found", ["search_term vazio"], qty))
                continue

            try:
                b.search(search)
            except Exception as e:
                item_reports.append(_blank_report(raw, "not_found", [f"erro navegação: {e}"], qty))
                continue

            results = b.get_results()
            if not results:
                fallback = search.split()[0] if search.split() else search
                if fallback != search:
                    try:
                        b.search(fallback)
                        results = b.get_results()
                    except Exception:
                        pass
            if not results:
                item_reports.append(_blank_report(raw, "not_found", ["nenhum resultado"], qty))
                continue

            outcome = select(item, results, config)

            if isinstance(outcome, NoResult):
                item_reports.append(_blank_report(raw, "not_found", [outcome.reason], qty))
                continue

            if isinstance(outcome, Ambiguity):
                if use_llm and adapter is not None:
                    idx, reason = _resolve_ambiguity_llm(outcome, item, adapter)
                    if idx is None:
                        decision = _auto_resolve_ambiguity(outcome, item, config)
                        status = "ambiguous_auto"
                        decision.notes.append(f"LLM falhou → auto: {reason}")
                    else:
                        chosen = next((r for r in results if r.index == idx), None)
                        if not chosen:
                            decision = _auto_resolve_ambiguity(outcome, item, config)
                            status = "ambiguous_auto"
                            decision.notes.append("índice LLM inválido → auto")
                        else:
                            packs, total = _packs_for(chosen, item, config)
                            decision = Decision(
                                index=chosen.index, name=chosen.name, price_num=chosen.price_num,
                                rule=f"llm-resolved ({outcome.kind})",
                                packs_needed=packs, total_cost=total,
                                notes=[f"LLM: {reason}"],
                            )
                            status = "ambiguous_resolved"
                else:
                    decision = _auto_resolve_ambiguity(outcome, item, config)
                    status = "ambiguous_auto"
            else:
                decision = outcome  # type: ignore[assignment]
                status = "ok"

            if dry_run:
                item_reports.append(_decision_to_report(raw, decision, qty, status, dry_run=True))
                continue

            try:
                set_result = b.set_qty(decision.index, decision.packs_needed)
                if not set_result.get("success"):
                    item_reports.append(
                        _decision_to_report(
                            raw, decision, qty, "failed_to_add",
                            extra_notes=[f"set_qty falhou: {set_result.get('error', '?')}"],
                        )
                    )
                    if set_result.get("rate_limit_pause"):
                        print("[main] sem ack — pausa 4s antes do próximo item", flush=True)
                        b.page.wait_for_timeout(4000)
                    continue
                # Folga para o carrinho gravar no servidor antes da próxima busca.
                b.page.wait_for_timeout(800)
            except Exception as e:
                item_reports.append(
                    _decision_to_report(
                        raw, decision, qty, "failed_to_add",
                        extra_notes=[f"exceção set_qty: {e}"],
                    )
                )
                continue

            item_reports.append(_decision_to_report(raw, decision, qty, status))

        try:
            badge_after = b.cart_badge()
        except Exception:
            badge_after = None

    elapsed = time.time() - started

    report_md = render_report(
        list_path=str(list_path),
        profile_path=str(profile_path),
        items=item_reports,
        cart_badge_before=badge_before,
        cart_badge_after=badge_after,
        elapsed_s=elapsed,
    )
    report_out.write_text(report_md, encoding="utf-8")
    print(f"[main] Relatório salvo em {report_out} ({elapsed:.1f}s total)", flush=True)
    return 0


def _decision_to_report(
    raw: str,
    d: Decision,
    qty_target: int | float,
    status: str,
    dry_run: bool = False,
    extra_notes: list[str] | None = None,
) -> ItemReport:
    notes = list(d.notes)
    if dry_run:
        notes.append("dry-run (não adicionado)")
    if extra_notes:
        notes.extend(extra_notes)
    return ItemReport(
        raw=raw,
        status=status,
        chosen_name=d.name,
        chosen_index=d.index,
        rule=d.rule,
        qty_target=qty_target,
        packs_added=d.packs_needed,
        unit_price=d.price_num,
        total_cost=d.total_cost,
        notes=notes,
    )


def _blank_report(raw: str, status: str, notes: list[str], qty_target) -> ItemReport:
    return ItemReport(
        raw=raw, status=status, chosen_name=None, chosen_index=None, rule=None,
        qty_target=qty_target, packs_added=0, unit_price=0.0, total_cost=0.0,
        notes=notes,
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Andorinha Shopping Orchestrator v2.1 (LLM opcional)")
    p.add_argument("--list", required=True, type=Path, help="Lista de compras MD/TXT")
    p.add_argument("--profile", required=True, type=Path, help="perfil-compras.yaml")
    p.add_argument(
        "--report-out",
        type=Path,
        default=Path(tempfile.gettempdir()) / "andorinha_report.md",
    )
    p.add_argument("--dry-run", action="store_true", help="Simula sem adicionar ao carrinho")
    p.add_argument("--launch-own", action="store_true", default=True,
                   help="Lança Chromium próprio (default True)")
    p.add_argument("--cdp", action="store_true",
                   help="Usa Chrome existente via CDP (desliga launch-own)")
    p.add_argument("--use-llm", action="store_true", default=True,
                   help="Usa grok -p nas ambiguidades (default ligado)")
    p.add_argument("--no-llm", action="store_true",
                   help="Não chama grok -p; auto-resolve marca preferida")
    p.add_argument("--keep-open", action="store_true", default=True,
                   help="Mantém o browser aberto no final para você fechar a compra")
    p.add_argument("--no-keep-open", action="store_true",
                   help="Fecha o browser ao terminar")
    args = p.parse_args()

    launch_own = not args.cdp
    return run(
        list_path=args.list,
        profile_path=args.profile,
        report_out=args.report_out,
        dry_run=args.dry_run,
        launch_own=launch_own,
        use_llm=(not args.no_llm),
        keep_open=(not args.no_keep_open),
    )


if __name__ == "__main__":
    raise SystemExit(main())
