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
import json
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


def _search_results(b: Browser, search: str) -> list[ProductResult]:
    b.search(search)
    results = b.get_results()
    if results:
        return results
    fallback = search.split()[0] if search.split() else search
    if fallback != search:
        try:
            b.search(fallback)
            return b.get_results()
        except Exception:
            return []
    return []


def _cand_payload(results: list[ProductResult]) -> list[dict]:
    return [
        {
            "index": r.index,
            "name": r.name,
            "price_num": r.price_num,
            "price_per_base_unit": r.price_per_base_unit,
            "price_base_dim": r.price_base_dim,
        }
        for r in results[:12]
    ]


def _match_name(results: list[ProductResult], name: str | None) -> ProductResult | None:
    if not name:
        return None
    want = name.lower()
    for r in results:
        if r.name_lower == want:
            return r
    for r in results:
        if want[:48] in r.name_lower or r.name_lower[:48] in want:
            return r
    return None


def _planned_from_decision(item: dict, decision: Decision, status: str, results: list[ProductResult]) -> dict:
    return {
        "raw": item.get("raw", ""),
        "item_text": item.get("item_text"),
        "search_term": item.get("search_term", ""),
        "qty": item.get("qty", 1),
        "unit": item.get("unit"),
        "flavor": item.get("flavor"),
        "status": status,
        "chosen_index": decision.index,
        "chosen_name": decision.name,
        "packs_needed": decision.packs_needed,
        "price_num": decision.price_num,
        "total_cost": decision.total_cost,
        "rule": decision.rule,
        "notes": list(decision.notes),
        "candidates": _cand_payload(results),
    }


def _plan_one(b: Browser, item: dict, config: SelectorConfig) -> dict:
    raw = item.get("raw", "")
    search = (item.get("search_term") or "").strip()
    qty = item.get("qty", 1)
    base = {
        "raw": raw,
        "item_text": item.get("item_text"),
        "search_term": search,
        "qty": qty,
        "unit": item.get("unit"),
        "flavor": item.get("flavor"),
        "status": "not_found",
        "chosen_index": None,
        "chosen_name": None,
        "packs_needed": 0,
        "price_num": 0,
        "total_cost": 0,
        "rule": None,
        "notes": [],
        "candidates": [],
    }
    if not search:
        base["notes"] = ["search_term vazio"]
        return base
    try:
        results = _search_results(b, search)
    except Exception as e:
        base["notes"] = [f"erro navegação: {e}"]
        return base
    if not results:
        base["notes"] = ["nenhum resultado"]
        return base
    base["candidates"] = _cand_payload(results)
    outcome = select(item, results, config)
    if isinstance(outcome, NoResult):
        base["notes"] = [outcome.reason]
        return base
    if isinstance(outcome, Ambiguity):
        base["status"] = "needs_grok"
        base["ambiguity_kind"] = outcome.kind
        base["ambiguity_reason"] = outcome.reason
        if outcome.preferred:
            base["preferred_index"] = outcome.preferred.index
            base["preferred_name"] = outcome.preferred.name
        base["notes"] = [outcome.reason]
        return base
    return _planned_from_decision(item, outcome, "decided", results)


def _resolve_needs_grok(
    planned: dict,
    item: dict,
    results: list[ProductResult],
    adapter: LLMAdapter | None,
    config: SelectorConfig,
) -> dict:
    from .selector import Ambiguity as Amb

    cands = []
    for c in planned.get("candidates") or []:
        match = next((r for r in results if r.index == c.get("index") or r.name == c.get("name")), None)
        if match:
            cands.append(match)
    if not cands:
        cands = results
    pref = None
    if planned.get("preferred_index") is not None:
        pref = next((r for r in cands if r.index == planned["preferred_index"]), None)
    ambiguity = Amb(
        kind=planned.get("ambiguity_kind") or "dominance",
        item_raw=planned.get("raw", ""),
        reason=planned.get("ambiguity_reason") or "",
        candidates=cands[:5],
        preferred=pref,
    )
    if adapter is None:
        decision = _auto_resolve_ambiguity(ambiguity, item, config)
        return _planned_from_decision(item, decision, "ambiguous_auto", results)
    idx, reason = _resolve_ambiguity_llm(ambiguity, item, adapter)
    if idx is None:
        decision = _auto_resolve_ambiguity(ambiguity, item, config)
        decision.notes.append(f"LLM falhou → auto: {reason}")
        return _planned_from_decision(item, decision, "ambiguous_auto", results)
    chosen = next((r for r in results if r.index == idx), None) or _match_name(results, None)
    chosen = chosen or next((r for r in cands if r.index == idx), None)
    if not chosen:
        decision = _auto_resolve_ambiguity(ambiguity, item, config)
        decision.notes.append("índice LLM inválido → auto")
        return _planned_from_decision(item, decision, "ambiguous_auto", results)
    packs, total = _packs_for(chosen, item, config)
    decision = Decision(
        index=chosen.index,
        name=chosen.name,
        price_num=chosen.price_num,
        rule=f"llm-resolved ({ambiguity.kind})",
        packs_needed=packs,
        total_cost=total,
        notes=[f"LLM: {reason}"],
    )
    return _planned_from_decision(item, decision, "ambiguous_resolved", results)


def _apply_one(b: Browser, planned: dict, item: dict, config: SelectorConfig) -> ItemReport:
    raw = planned.get("raw") or item.get("raw", "")
    qty = planned.get("qty") or item.get("qty") or 1
    if planned.get("status") in (None, "not_found", "needs_grok"):
        return _blank_report(raw, "not_found", planned.get("notes") or ["sem decisão"], qty)
    search = (planned.get("search_term") or item.get("search_term") or "").strip()
    try:
        results = _search_results(b, search)
    except Exception as e:
        return _blank_report(raw, "not_found", [f"erro navegação: {e}"], qty)
    chosen = _match_name(results, planned.get("chosen_name"))
    if chosen is None and planned.get("chosen_index") is not None:
        chosen = next((r for r in results if r.index == planned["chosen_index"]), None)
    if chosen is None:
        return _blank_report(raw, "not_found", ["escolha não está mais nos resultados"], qty)
    packs = int(planned.get("packs_needed") or 1)
    state = b.card_state(chosen.index)
    current = int(state.get("qty") or 0)
    need = max(0, packs - current)
    decision = Decision(
        index=chosen.index,
        name=chosen.name,
        price_num=float(planned.get("price_num") or chosen.price_num),
        rule=str(planned.get("rule") or "apply"),
        packs_needed=packs,
        total_cost=float(planned.get("total_cost") or 0),
        notes=list(planned.get("notes") or []),
    )
    if need == 0 and state.get("has_minus"):
        decision.notes.append("já no alvo — sem clique extra")
        return _decision_to_report(raw, decision, qty, planned.get("status") or "ok")
    try:
        set_result = b.set_qty(chosen.index, need)
        if not set_result.get("success"):
            extra = [f"set_qty falhou: {set_result.get('error', '?')}"]
            if set_result.get("rate_limit_pause"):
                print("[main] sem ack — pausa 4s antes do próximo item", flush=True)
                b.page.wait_for_timeout(4000)
            return _decision_to_report(
                raw, decision, qty, "failed_to_add", extra_notes=extra
            )
        b.page.wait_for_timeout(800)
    except Exception as e:
        return _decision_to_report(
            raw, decision, qty, "failed_to_add", extra_notes=[f"exceção set_qty: {e}"]
        )
    return _decision_to_report(raw, decision, qty, planned.get("status") or "ok")


def run(
    list_path: Path,
    profile_path: Path,
    report_out: Path,
    dry_run: bool = False,
    launch_own: bool = True,
    use_llm: bool = False,
    keep_open: bool = True,
    command: str = "run",
    run_file: Path | None = None,
) -> int:
    started = time.time()
    run_file = run_file or Path("run.json")
    command = (command or "run").lower()

    print(f"[main] Enriquecendo lista: {list_path.name}", flush=True)
    enriched = enrich_list(list_path, profile_path)
    items = enriched["items"]
    print(
        f"[main] {enriched['meta']['total_items']} itens "
        f"({enriched['meta']['kb_matched']} matched, "
        f"{enriched['meta']['unmatched']} unmatched) cmd={command}",
        flush=True,
    )

    config, raw_cfg = _load_config(profile_path)
    llm_model = str(raw_cfg.get("llm_model", "grok-build"))
    llm_timeout = int(raw_cfg.get("llm_timeout_s", 120))
    browser_cfg = raw_cfg.get("browser", {}) or {}
    cdp_url = str(browser_cfg.get("cdp_url", "http://localhost:9222"))
    click_settle_ms = int(browser_cfg.get("click_settle_ms", 250))
    nav_timeout_ms = int(browser_cfg.get("nav_timeout_ms", 15000))

    adapter: LLMAdapter | None = None
    if use_llm and command != "apply":
        adapter = GrokCLIAdapter(model=llm_model, timeout_s=llm_timeout)
        print("[main] LLM habilitado (grok -p)", flush=True)
    else:
        print("[main] LLM desligado neste comando", flush=True)

    item_reports: list[ItemReport] = []
    do_click = command == "apply" or (command == "run" and not dry_run)

    print(f"[main] Conectando browser (click={do_click}, launch_own={launch_own})", flush=True)
    try:
        browser_ctx = Browser(
            cdp_url=cdp_url,
            click_settle_ms=click_settle_ms,
            nav_timeout_ms=nav_timeout_ms,
            launch_own=launch_own,
            keep_open=bool(keep_open and do_click),
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

        planned_list: list[dict]
        if command == "apply" and run_file.is_file():
            payload = json.loads(run_file.read_text(encoding="utf-8"))
            planned_list = payload.get("items") or []
            print(f"[main] apply a partir de {run_file} ({len(planned_list)} itens)", flush=True)
        else:
            planned_list = []
            for n, item in enumerate(items, 1):
                print(
                    f"[main] [plan {n:2d}/{len(items)}] {str(item.get('raw', ''))[:35]:<35} "
                    f"→ {str(item.get('search_term', ''))[:35]}",
                    flush=True,
                )
                planned_list.append(_plan_one(b, item, config))

            if command in ("run", "plan") and use_llm and adapter is not None:
                for n, (item, planned) in enumerate(zip(items, planned_list), 1):
                    if planned.get("status") != "needs_grok":
                        continue
                    print(f"[main] [grok {n}] {planned.get('raw', '')[:40]}", flush=True)
                    try:
                        results = _search_results(b, planned.get("search_term") or "")
                    except Exception:
                        results = []
                    planned_list[n - 1] = _resolve_needs_grok(
                        planned, item, results, adapter, config
                    )
            elif command == "run" and not use_llm:
                for n, (item, planned) in enumerate(zip(items, planned_list), 1):
                    if planned.get("status") != "needs_grok":
                        continue
                    try:
                        results = _search_results(b, planned.get("search_term") or "")
                    except Exception:
                        results = []
                    planned_list[n - 1] = _resolve_needs_grok(
                        planned, item, results, None, config
                    )

            run_file.write_text(
                json.dumps(
                    {"meta": enriched.get("meta"), "items": planned_list},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"[main] plano salvo em {run_file}", flush=True)

        if command == "plan" or dry_run:
            for item, planned in zip(items, planned_list):
                status = planned.get("status") or "not_found"
                if status == "decided":
                    status = "ok"
                if planned.get("chosen_name"):
                    d = Decision(
                        index=int(planned.get("chosen_index") or 0),
                        name=planned["chosen_name"],
                        price_num=float(planned.get("price_num") or 0),
                        rule=str(planned.get("rule") or "-"),
                        packs_needed=int(planned.get("packs_needed") or 0),
                        total_cost=float(planned.get("total_cost") or 0),
                        notes=list(planned.get("notes") or []),
                    )
                    item_reports.append(
                        _decision_to_report(
                            planned.get("raw") or "", d, planned.get("qty") or 1,
                            status, dry_run=True,
                        )
                    )
                else:
                    item_reports.append(
                        _blank_report(
                            planned.get("raw") or "",
                            "not_found" if status != "needs_grok" else "ambiguous_unresolved",
                            planned.get("notes") or [],
                            planned.get("qty") or 1,
                        )
                    )
        else:
            apply_items = items if command != "apply" else [{}] * len(planned_list)
            if command == "apply":
                apply_items = [{} for _ in planned_list]
            else:
                apply_items = items
            for n, (item, planned) in enumerate(zip(apply_items, planned_list), 1):
                print(
                    f"[main] [apply {n:2d}/{len(planned_list)}] {str(planned.get('raw', ''))[:40]}",
                    flush=True,
                )
                item_reports.append(_apply_one(b, planned, item or {}, config))

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
    p = argparse.ArgumentParser(description="Andorinha Shopping Orchestrator v3 (plan/apply/run)")
    p.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["plan", "apply", "run"],
        help="plan=só busca, apply=só clica run.json, run=plan+grok+apply",
    )
    p.add_argument("--list", required=True, type=Path, help="Lista de compras MD/TXT")
    p.add_argument("--profile", required=True, type=Path, help="perfil-compras.yaml")
    p.add_argument("--run-file", type=Path, default=Path("run.json"), help="plano JSON")
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
        command=args.command,
        run_file=args.run_file,
    )


if __name__ == "__main__":
    raise SystemExit(main())
