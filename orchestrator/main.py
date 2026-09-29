"""
main.py — Orquestrador end-to-end da Andorinha Shopping Skill (v2.1).

Mudanças vs v2 original:
- LLM é OPCIONAL (flag --use-llm). Default = zero chamada de modelo.
- Quando Ambiguity e LLM desligado: auto-resolve (preferred ou cheapest).
- --launch-own como default prático para Grok Build / ambientes sem CDP.

CLI:
    python3 -m orchestrator.main \\
        --list lista-compras.md \\
        --profile preferencias.yaml \\
        --report-out /tmp/relatorio.md \\
        [--dry-run] [--use-llm] [--launch-own]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import yaml

from .browser import Browser, BrowserError, ProductResult
from .decision.jev import (
    JevAdvisor,
    JevError,
    compare_to_user_choice,
    is_confident_suggestion,
)
from .enricher import (
    apply_catalog_mapping,
    catalog_candidates,
    enrich_list,
    load_profile,
    normalize,
)
from .llm.adapter import (
    GrokCLIAdapter,
    LLMAdapter,
    LLMCallError,
    render_ambiguity_prompt,
)
from .report import ItemReport, reconcile_with_cart, render_duvidas, render_report
from .selector import (
    packs_for_qty,
    Ambiguity,
    Decision,
    NoResult,
    SelectorConfig,
    is_lactose_free_label,
    select,
)


PROMPTS_DIR = Path(__file__).parent / "llm" / "prompts"


def _load_config(profile_path: Path) -> tuple[SelectorConfig, dict]:
    data = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    cfg = data.get("config", {}) or {}
    if "sem_lactose" in data:
        sl = bool(data.get("sem_lactose"))
    else:
        sl = bool((data.get("household") or {}).get("lactose_free", False))
    return SelectorConfig(
        dominance_threshold_pct=float(cfg.get("dominance_threshold_pct", 15.0)),
        pack_optimization=bool(cfg.get("pack_optimization", True)),
        max_packs_per_item=int(cfg.get("max_packs_per_item", 6)),
        household_lactose_free=sl,
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
        user_answer=str(enriched_item.get("user_answer") or ""),
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


def _search_results(
    b: Browser, search: str, *, lactose_free: bool = False
) -> list[ProductResult]:
    b.search(search)
    results = b.get_results()
    if lactose_free and results and not any(is_lactose_free_label(r.name_lower) for r in results):
        seen = {r.name_lower for r in results}
        for suffix in (" zero lactose", " sem lactose"):
            try:
                b.search(f"{search}{suffix}")
                extra = b.get_results()
            except Exception:
                extra = []
            for r in extra:
                if r.name_lower not in seen:
                    results.append(r)
                    seen.add(r.name_lower)
            if any(is_lactose_free_label(r.name_lower) and "iogurte" in r.name_lower for r in results):
                break
    return results


def _cand_payload(results: list[ProductResult]) -> list[dict]:
    return [
        {
            "index": r.index,
            "name": r.name,
            "price_num": r.price_num,
            "price_per_base_unit": r.price_per_base_unit,
            "price_base_dim": r.price_base_dim,
            "sale_unit": r.sale_unit,
            "categories": (r.categories or [])[:3],
            "brand_name": r.brand_name,
            "product_id": r.product_id,
        }
        for r in results[:12]
    ]


_MATCH_STOP = frozenset(
    "com sem para tipo longa vida embalagem pacote caixa und kg".split()
)


def _name_tokens(name: str) -> set[str]:
    from .enricher import normalize

    return {t for t in normalize(name).split() if len(t) >= 4 and t not in _MATCH_STOP}


def _match_name(results: list[ProductResult], name: str | None) -> ProductResult | None:
    """Casa pelo nome. Não aceita 'requeijão' qualquer no lugar da marca pedida."""
    if not name:
        return None
    want = name.lower()
    for r in results:
        if r.name_lower == want:
            return r
    for r in results:
        if want[:48] in r.name_lower:
            return r
        # "Creme Piracanjuba" ⊂ "Creme S/Lactose Piracanjuba" — não serve
        if r.name_lower[:48] in want:
            extra = _name_tokens(name) - _name_tokens(r.name)
            if not extra:
                return r
    want_toks = _name_tokens(name)
    if not want_toks:
        return None
    best = None
    best_n = 0
    for r in results:
        n = len(want_toks & _name_tokens(r.name))
        if n > best_n:
            best, best_n = r, n
    need = min(3, len(want_toks))
    if best is not None and best_n >= need:
        return best
    return None


def _unit_from_chosen(item: dict, results: list[ProductResult], decision: Decision) -> str:
    chosen = next((r for r in results if r.index == decision.index), None)
    if chosen is None:
        chosen = _match_name(results, decision.name)
    if chosen is not None:
        sale = (chosen.sale_unit or "").upper()
        if sale == "KG" or chosen.sell_by_weight or chosen.price_per_kg:
            return "kg"
        if sale == "UN":
            return "un"
    return item.get("unit") or "un"


def _planned_from_decision(item: dict, decision: Decision, status: str, results: list[ProductResult]) -> dict:
    chosen_result = next((r for r in results if r.index == decision.index), None)
    planned = {
        "raw": item.get("raw", ""),
        "item_text": item.get("item_text"),
        "search_term": item.get("search_term", ""),
        "qty": item.get("qty", 1),
        "unit": _unit_from_chosen(item, results, decision),
        "flavor": item.get("flavor"),
        "status": status,
        "chosen_index": decision.index,
        "chosen_name": decision.name,
        "chosen_product_id": chosen_result.product_id if chosen_result else None,
        "packs_needed": decision.packs_needed,
        "price_num": decision.price_num,
        "total_cost": decision.total_cost,
        "rule": decision.rule,
        "notes": list(decision.notes),
        "candidates": _cand_payload(results),
    }
    if item.get("jev_enrichment") is not None:
        planned["jev_enrichment"] = item["jev_enrichment"]
    return planned


def _index_from_answer(text: str) -> int | None:
    t = (text or "").strip()
    if re.fullmatch(r"\d+", t):
        return int(t)
    return None


def _is_skip_answer(text: str) -> bool:
    from .enricher import normalize

    t = normalize(text)
    return t in {"pular", "pula", "skip", "nao", "nao quero"} or t.startswith("pular ")


def _apply_answers_file(planned_list: list[dict], answers_path: Path | None) -> int:
    """Copia respostas do YAML para planned[i].user_answer. Retorna quantas casaram."""
    n = 0
    if answers_path and answers_path.is_file():
        data = yaml.safe_load(answers_path.read_text(encoding="utf-8")) or {}
        rows = data.get("respostas") if isinstance(data, dict) else None
        if rows is None and isinstance(data, dict):
            rows = [{"item": k, "texto": v} for k, v in data.items() if k != "respostas"]
        if not isinstance(rows, list):
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            needle = str(row.get("item") or row.get("raw") or "").lower()
            texto = str(row.get("texto") or row.get("answer") or "").strip()
            if not needle or not texto:
                continue
            for it in planned_list:
                raw = str(it.get("raw") or "").lower()
                if needle in raw or raw in needle:
                    it["user_answer"] = texto
                    n += 1
                    break
    return n


def _format_duvida_block(it: dict, n: int, total: int) -> str:
    lines = [
        "",
        f"—— {n}/{total}  {it.get('raw') or '?'} ——",
        f"Busca: {it.get('search_term') or '—'}",
        f"Qty: {it.get('qty') or 1} {it.get('unit') or ''}".rstrip(),
        "Por quê: " + " · ".join(it.get("notes") or ["não decidido"]),
    ]
    cands = it.get("candidates") or []
    if cands:
        lines.append("O que apareceu:")
        for c in cands[:8]:
            price = c.get("price_num")
            price_s = f"R${price:.2f}" if isinstance(price, (int, float)) else "?"
            lines.append(f"  [{c.get('index')}] {c.get('name')} — {price_s}")
    else:
        lines.append("O que apareceu: nada")
    return "\n".join(lines)


def _prompt_duvidas(planned_list: list[dict], input_fn=input) -> int:
    """Pergunta no terminal. Enter = deixa pra depois. 'pular' = não comprar."""
    pending = [p for p in planned_list if p.get("status") == "needs_user"]
    if not pending:
        return 0
    print("\n===== Dúvidas — nada disso foi para o carrinho =====", flush=True)
    print("Responda em uma linha. Enter = deixar. pular = não comprar.", flush=True)
    answered = 0
    for i, it in enumerate(pending, 1):
        print(_format_duvida_block(it, i, len(pending)), flush=True)
        try:
            ans = input_fn(
                f"[{i}/{len(pending)}] O que fazer? "
            )
        except EOFError:
            print("[main] sem terminal — parando as perguntas.", flush=True)
            break
        ans = (ans or "").strip()
        if ans:
            it["user_answer"] = ans
            answered += 1
    return answered


def _resolve_answered(
    b: Browser,
    planned_list: list[dict],
    items: list[dict],
    adapter: LLMAdapter | None,
    config: SelectorConfig,
) -> None:
    by_raw = {str(it.get("raw") or ""): it for it in items}
    for n, planned in enumerate(planned_list, 1):
        if planned.get("status") != "needs_user":
            continue
        ans = str(planned.get("user_answer") or "").strip()
        if not ans:
            continue
        if _is_skip_answer(ans):
            planned["status"] = "not_found"
            planned["notes"] = list(planned.get("notes") or []) + [f"você: pular ({ans})"]
            print(f"[main] [resolve {n}] {planned.get('raw')} → pulado", flush=True)
            continue
        idx = _index_from_answer(ans)
        if idx is not None:
            hit = next(
                (c for c in (planned.get("candidates") or []) if c.get("index") == idx),
                None,
            )
            if hit and hit.get("name"):
                price = float(hit.get("price_num") or 0)
                qty = planned.get("qty") or 1
                unit = (planned.get("unit") or "un").lower()
                packs = packs_for_qty(qty, unit)
                planned.update(
                    {
                        "status": "decided",
                        "chosen_index": idx,
                        "chosen_name": hit["name"],
                        "chosen_product_id": hit.get("product_id"),
                        "price_num": price,
                        "packs_needed": packs,
                        "total_cost": round(price * (1 if unit in ("kg", "g") else packs), 2),
                        "rule": "user-index",
                        "notes": list(planned.get("notes") or []) + [f"você escolheu [{idx}]"],
                    }
                )
                comparison = compare_to_user_choice(planned.get("jev"), idx)
                if comparison is not None:
                    planned["jev_user_comparison"] = comparison
                print(f"[main] [resolve {n}] {planned.get('raw')} → [{idx}] {hit['name'][:50]}", flush=True)
                continue
        item = by_raw.get(str(planned.get("raw") or "")) or {}
        item = dict(item)
        item["user_answer"] = ans
        extra = ans.strip()
        search = (planned.get("search_term") or item.get("search_term") or "").strip()
        search = re.sub(r"\s+\d+$", "", search).strip()
        if not extra.isdigit() and extra.lower() not in search.lower():
            search = f"{search} {extra}".strip()
        print(f"[main] [resolve {n}] {planned.get('raw')} ← {ans[:50]}", flush=True)
        try:
            results = _search_results(
                b, search, lactose_free=bool(item.get("lactose_free")),
            )
        except Exception:
            results = []
        planned["search_term"] = search
        planned_list[n - 1] = _resolve_needs_grok(
            planned, item, results, adapter, config
        )


def _plan_one(
    b: Browser, item: dict, config: SelectorConfig, jev_queue: list[dict] | None = None
) -> dict:
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
    if item.get("jev_enrichment") is not None:
        base["jev_enrichment"] = item["jev_enrichment"]
    if not search:
        base["notes"] = ["search_term vazio"]
        return base
    try:
        results = _search_results(b, search, lactose_free=bool(item.get("lactose_free")))
    except Exception as e:
        base["notes"] = [f"erro navegação: {e}"]
        return base
    if not results:
        base["notes"] = ["nenhum resultado"]
        return base
    base["candidates"] = _cand_payload(results)
    outcome = select(item, results, config)
    if isinstance(outcome, NoResult):
        base["status"] = "needs_user"
        base["notes"] = [outcome.reason]
        return base
    if isinstance(outcome, Ambiguity):
        base["status"] = "needs_user"
        base["ambiguity_kind"] = outcome.kind
        base["ambiguity_reason"] = outcome.reason
        if outcome.preferred:
            base["preferred_index"] = outcome.preferred.index
            base["preferred_name"] = outcome.preferred.name
        base["notes"] = [outcome.reason]
        if jev_queue is not None:
            jev_queue.append({
                "planned": base,
                "item": item,
                "ambiguity": outcome,
                "results": results,
            })
        return base
    return _planned_from_decision(item, outcome, "decided", results)


def _jev_profile_pass(
    items: list[dict], profile: dict, jev: JevAdvisor, *, apply: bool,
    min_probability: float, min_margin: float,
) -> None:
    """Batch semantic enrichment for items the local profile did not recognize."""
    unresolved = [
        (index, item) for index, item in enumerate(items)
        if not item.get("kb_matched")
    ]
    entries: list[dict] = []
    refs: dict[str, tuple[int, dict]] = {}
    for index, item in unresolved:
        candidates = catalog_candidates(item, profile, limit=6)
        if not candidates:
            continue
        key = f"list_item_{index}"
        current = str(item.get("generic") or item.get("item_text") or item.get("raw") or "")
        brands = {normalize(k): str(v) for k, v in (profile.get("marcas") or {}).items()}
        quantities = {normalize(k): v for k, v in (profile.get("quantidades") or {}).items()}
        options = [{
            "id": "keep_current",
            "description": f"Manter o texto interpretado como está: {current}",
            "value": {"generic": current, "keep_current": True},
        }]
        for n, generic in enumerate(candidates):
            if normalize(generic) == normalize(current):
                continue
            options.append({
                "id": f"generic_{n}",
                "description": (
                    f"Produto cadastrado no perfil: {generic}; "
                    f"marca preferida={brands.get(normalize(generic), 'nenhuma')}; "
                    f"quantidade padrão={quantities.get(normalize(generic), 'não definida')}"
                ),
                "value": {"generic": generic, "keep_current": False},
            })
        entries.append({
            "id": key,
            "state": {
                "pedido": item.get("raw"),
                "texto_do_item": item.get("item_text"),
                "sabor": item.get("flavor"),
                "quantidade": item.get("qty"),
                "unidade": item.get("unit"),
                "secao": item.get("department"),
                "interpretacao_atual": current,
            },
            "question": (
                "Mapeie o pedido para o produto do perfil que representa o mesmo item. "
                "Não escolha apenas por palavras parecidas; respeite sabor, tipo e seção. "
                "Se nenhuma opção for claramente equivalente, escolha ask_user."
            ),
            "options": options,
        })
        refs[key] = (index, item)
    if not entries:
        return
    for start in range(0, len(entries), 12):
        group = entries[start : start + 12]
        try:
            suggestions = jev.choose_many(group)
        except JevError as exc:
            for entry in group:
                _, item = refs[entry["id"]]
                item["jev_enrichment"] = {"error": str(exc)}
            continue
        for key, suggestion in suggestions.items():
            index, item = refs[key]
            item["jev_enrichment"] = suggestion
            if not apply or not is_confident_suggestion(
                suggestion, min_probability=min_probability, min_margin=min_margin
            ):
                continue
            candidate = suggestion.get("candidate") or {}
            generic = candidate.get("generic")
            if not generic or candidate.get("keep_current"):
                continue
            try:
                probability = float(
                    (suggestion.get("probabilities") or {}).get(suggestion.get("choice")) or 0
                )
            except (TypeError, ValueError):
                probability = 0.0
            items[index] = apply_catalog_mapping(item, str(generic), profile, probability)
            items[index]["jev_enrichment"] = suggestion


def _jev_product_pass(
    queue: list[dict], jev: JevAdvisor, config: SelectorConfig, *,
    apply: bool, min_probability: float, min_margin: float,
) -> None:
    """Batch product judgments after every search and hard selector gate ran."""
    chunk_size = 12
    for start in range(0, len(queue), chunk_size):
        group = queue[start : start + chunk_size]
        entries: list[dict] = []
        refs: dict[str, dict] = {}
        for n, context in enumerate(group):
            planned = context["planned"]
            item = context["item"]
            ambiguity = context["ambiguity"]
            options = []
            for candidate_index, product in enumerate(ambiguity.candidates[:5]):
                option_id = f"product_{candidate_index}"
                options.append({
                    "id": option_id,
                    "description": (
                        f"{product.name}; marca={product.brand_name or 'desconhecida'}; "
                        f"R${product.price_num:.2f}; preço/base={product.price_per_base_unit:.4f} "
                        f"por {product.price_base_dim}; unidade={product.sale_unit or product.unit}"
                    ),
                    "value": {
                        "index": product.index,
                        "name": product.name,
                        "product_id": product.product_id,
                    },
                })
            key = f"product_item_{start + n}"
            entries.append({
                "id": key,
                "state": {
                    "pedido": item.get("raw"),
                    "produto_interpretado": item.get("item_text"),
                    "sabor": item.get("flavor"),
                    "quantidade": item.get("qty"),
                    "unidade": item.get("unit"),
                    "marca_preferida": item.get("preferred_brand"),
                    "sem_lactose": item.get("lactose_free"),
                    "tipo_de_duvida": ambiguity.kind,
                    "motivo": ambiguity.reason,
                },
                "question": (
                    "Escolha o resultado que corresponde ao pedido e às preferências. "
                    "Não troque sabor ou tipo por semelhança superficial. "
                    "Se a substituição for incerta, escolha ask_user."
                ),
                "options": options,
            })
            refs[key] = context
        try:
            suggestions = jev.choose_many(entries)
        except JevError as exc:
            for context in group:
                context["planned"]["jev"] = {"error": str(exc)}
            continue
        for key, suggestion in suggestions.items():
            context = refs[key]
            planned = context["planned"]
            planned["jev"] = suggestion
            if not apply or not is_confident_suggestion(
                suggestion, min_probability=min_probability, min_margin=min_margin
            ):
                continue
            selected_index = (suggestion.get("candidate") or {}).get("index")
            selected = next(
                (c for c in context["ambiguity"].candidates if c.index == selected_index), None
            )
            if selected is None:
                continue
            packs, total = _packs_for(selected, context["item"], config)
            decision = Decision(
                index=selected.index,
                name=selected.name,
                price_num=selected.price_num,
                rule="jev-resolved",
                packs_needed=packs,
                total_cost=total,
                notes=[
                    "Jev resolveu após o filtro do selector; "
                    f"prob={suggestion['probabilities'].get(suggestion['choice'], 0):.3f}"
                ],
            )
            resolved = _planned_from_decision(
                context["item"], decision, "decided", context["results"]
            )
            resolved["jev"] = suggestion
            planned.clear()
            planned.update(resolved)


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
        planned["notes"] = list(planned.get("notes") or []) + ["sem LLM e sem chute"]
        planned["status"] = "needs_user"
        return planned
    idx, reason = _resolve_ambiguity_llm(ambiguity, item, adapter)
    if idx is None:
        planned["notes"] = list(planned.get("notes") or []) + [f"LLM falhou: {reason}"]
        planned["status"] = "needs_user"
        return planned
    chosen = next((r for r in results if r.index == idx), None) or _match_name(results, None)
    chosen = chosen or next((r for r in cands if r.index == idx), None)
    if not chosen:
        planned["notes"] = list(planned.get("notes") or []) + ["índice LLM inválido"]
        planned["status"] = "needs_user"
        return planned
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
    if planned.get("status") in (None, "not_found", "needs_grok", "needs_user"):
        return _blank_report(raw, "not_found", planned.get("notes") or ["sem decisão"], qty)
    search = (planned.get("search_term") or item.get("search_term") or "").strip()
    try:
        results = _search_results(b, search)
    except Exception as e:
        return _blank_report(raw, "not_found", [f"erro navegação: {e}"], qty)
    # Index positions are page-local and search HTTP hits are not clickable.
    # Re-find the exact product after navigation, before touching the cart.
    product_id = str(planned.get("chosen_product_id") or "")
    clickable = [r for r in results if r.has_add or r.has_plus or r.has_minus]
    chosen = next((r for r in clickable if product_id and r.product_id == product_id), None)
    if chosen is None and not product_id:
        chosen = next((r for r in clickable if r.name_lower == str(planned.get("chosen_name") or "").lower()), None)
    if chosen is None and planned.get("chosen_name"):
        try:
            results = _search_results(b, planned["chosen_name"])
        except Exception:
            results = []
        clickable = [r for r in results if r.has_add or r.has_plus or r.has_minus]
        chosen = next((r for r in clickable if product_id and r.product_id == product_id), None)
        if chosen is None and not product_id:
            chosen = next((r for r in clickable if r.name_lower == str(planned.get("chosen_name") or "").lower()), None)
    if chosen is None:
        return _blank_report(raw, "not_found", ["produto exato não está disponível em um card clicável"], qty)
    planned_price = float(planned.get("price_num") or 0)
    # Price drops are fine; only an increase needs a new review.
    if planned_price <= 0 or chosen.price_num <= 0 or chosen.price_num > planned_price + 0.01:
        return _blank_report(raw, "failed_to_add", [f"preço subiu (R${planned_price:.2f} → R${chosen.price_num:.2f}); gere e revise um novo plano"], qty)
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
    unit = planned.get("unit") or item.get("unit")
    by_weight = (unit or "").lower().strip() in ("kg", "g", "grama", "gramas")
    if need == 0 and (state.get("has_minus") or current > 0):
        decision.notes.append("já no alvo — sem clique extra")
        return _decision_to_report(
            raw, decision, qty, planned.get("status") or "ok", unit=unit
        )
    try:
        set_result = b.set_qty(chosen.index, need, by_weight=by_weight)
        if set_result.get("rate_limit_pause") and not by_weight:
            print("[main] ack incompleto — pausa 1s", flush=True)
            b.page.wait_for_timeout(1000)
        if not set_result.get("success") and set_result.get("error") == "no_button":
            return _decision_to_report(
                raw, decision, qty, "failed_to_add",
                extra_notes=[f"set_qty: {set_result.get('error')}"],
                unit=unit,
            )
        if not set_result.get("success"):
            decision.notes.append(f"ack do card falhou ({set_result.get('error')}); conferir no carrinho")
        b.page.wait_for_timeout(120)
    except Exception as e:
        return _decision_to_report(
            raw, decision, qty, "failed_to_add",
            extra_notes=[f"exceção set_qty: {e}"],
            unit=unit,
        )
    return _decision_to_report(
        raw, decision, qty, planned.get("status") or "ok", unit=unit
    )


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
    answers_path: Path | None = None,
    jev_shadow: bool = False,
    jev_auto: bool = False,
    jev_min_probability: float = 0.90,
    jev_min_margin: float = 0.20,
) -> int:
    started = time.time()
    run_file = run_file or Path("run.json")
    command = (command or "run").lower()

    print(f"[main] Enriquecendo lista: {list_path.name}", flush=True)
    enriched = enrich_list(list_path, profile_path)
    items = enriched["items"]
    config, raw_cfg = _load_config(profile_path)
    try:
        use_jev = (jev_shadow or jev_auto) and command not in ("apply", "resolve")
        jev = JevAdvisor() if use_jev else None
    except JevError as exc:
        print(f"[main] ERRO: {exc}", file=sys.stderr)
        return 2
    if jev is not None and command not in ("apply", "resolve"):
        mode = "auto" if jev_auto else "shadow"
        print(f"[main] Jev ativo em modo {mode}; decisões agrupadas por etapa", flush=True)
        try:
            _jev_profile_pass(
                items,
                load_profile(profile_path),
                jev,
                apply=jev_auto,
                min_probability=jev_min_probability,
                min_margin=jev_min_margin,
            )
        except JevError as exc:
            print(f"[main] aviso Jev no enriquecimento: {exc}", flush=True)
    enriched["meta"]["kb_matched"] = sum(bool(item.get("kb_matched")) for item in items)
    enriched["meta"]["unmatched"] = len(items) - enriched["meta"]["kb_matched"]
    print(
        f"[main] {enriched['meta']['total_items']} itens "
        f"({enriched['meta']['kb_matched']} matched, "
        f"{enriched['meta']['unmatched']} unmatched) cmd={command}",
        flush=True,
    )
    llm_model = str(raw_cfg.get("llm_model") or "grok-4.6")
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
    cart_lines_count: int | None = None
    cart_matched: int | None = None

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
        if command in ("apply", "resolve") and run_file.is_file():
            payload = json.loads(run_file.read_text(encoding="utf-8"))
            planned_list = payload.get("items") or []
            print(f"[main] {command} a partir de {run_file} ({len(planned_list)} itens)", flush=True)
        else:
            planned_list = []
            jev_queue: list[dict] = []
            for n, item in enumerate(items, 1):
                print(
                    f"[main] [plan {n:2d}/{len(items)}] {str(item.get('raw', ''))[:35]:<35} "
                    f"→ {str(item.get('search_term', ''))[:35]}",
                    flush=True,
                )
                planned_list.append(_plan_one(
                    b, item, config, jev_queue if jev is not None else None
                ))
            if jev is not None and jev_queue:
                _jev_product_pass(
                    jev_queue,
                    jev,
                    config,
                    apply=jev_auto,
                    min_probability=jev_min_probability,
                    min_margin=jev_min_margin,
                )
            run_file.write_text(
                json.dumps(
                    {"meta": enriched.get("meta"), "items": planned_list},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"[main] plano salvo em {run_file} (sem LLM, sem clique nas dúvidas)", flush=True)

        if command == "resolve":
            matched = _apply_answers_file(planned_list, answers_path)
            print(f"[main] {matched} resposta(s) casadas", flush=True)
            _resolve_answered(b, planned_list, items, adapter, config)
            run_file.write_text(
                json.dumps(
                    {"meta": enriched.get("meta"), "items": planned_list},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        pending = [p for p in planned_list if p.get("status") == "needs_user"]
        if pending and command in ("plan", "run") and sys.stdin.isatty():
            n_ans = _prompt_duvidas(planned_list)
            if n_ans:
                _resolve_answered(b, planned_list, items, adapter, config)
                run_file.write_text(
                    json.dumps(
                        {"meta": enriched.get("meta"), "items": planned_list},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            pending = [p for p in planned_list if p.get("status") == "needs_user"]

        if pending:
            duvidas_path = Path("duvidas.md")
            duvidas_path.write_text(render_duvidas(planned_list), encoding="utf-8")
            print(
                f"[main] {len(pending)} dúvida(s) ainda em {duvidas_path}.",
                flush=True,
            )
            if command != "apply":
                b.keep_open = False

        stop_apply = (
            command in ("plan", "resolve")
            or dry_run
            or bool(pending)
        )
        if stop_apply:
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
                            status, dry_run=True, unit=planned.get("unit"),
                        )
                    )
                else:
                    item_reports.append(
                        _blank_report(
                            planned.get("raw") or "",
                            "needs_user" if status in ("needs_user", "needs_grok") else "not_found",
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

            cart_lines: list[dict] = []
            try:
                cart_lines = b.read_cart_lines()
            except Exception as e:
                print(f"[main] scrape do carrinho falhou: {e}", flush=True)
            item_reports, cart_matched = reconcile_with_cart(item_reports, cart_lines)
            cart_lines_count = len(cart_lines)

        try:
            badge_after = b.cart_badge()
        except Exception:
            badge_after = None

        # Relatório antes do keep_open — senão só grava depois de 30 min.
        elapsed = time.time() - started
        report_md = render_report(
            list_path=str(list_path),
            profile_path=str(profile_path),
            items=item_reports,
            cart_badge_before=badge_before,
            cart_badge_after=badge_after,
            elapsed_s=elapsed,
            cart_lines_count=cart_lines_count if do_click else None,
            cart_matched=cart_matched if do_click else None,
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
    unit: str | None = None,
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
        unit=unit,
    )


def _blank_report(
    raw: str, status: str, notes: list[str], qty_target, unit: str | None = None
) -> ItemReport:
    return ItemReport(
        raw=raw, status=status, chosen_name=None, chosen_index=None, rule=None,
        qty_target=qty_target, packs_added=0, unit_price=0.0, total_cost=0.0,
        notes=notes, unit=unit,
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Andorinha Shopping Orchestrator v3 (plan/apply/run)")
    p.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["plan", "apply", "run", "resolve"],
        help="plan=busca sem chute; resolve=suas respostas+LLM; apply=clica; run=plan e para se houver dúvida",
    )
    p.add_argument("--list", required=True, type=Path, help="Lista de compras MD/TXT")
    p.add_argument("--profile", type=Path, default=Path("preferencias.yaml"), help="preferencias.yaml")
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
    p.add_argument("--answers", type=Path, default=None,
                   help="respostas.yaml para o comando resolve")
    jev_group = p.add_mutually_exclusive_group()
    jev_group.add_argument("--jev-shadow", action="store_true",
                           help="Registra sugestões Jev sem mudar escolhas ou carrinho")
    jev_group.add_argument("--jev-auto", action="store_true",
                           help="Permite mapeamento e decisões Jev com alta confiança")
    jev_group.add_argument("--no-jev", action="store_true",
                           help="Desativa o Jev mesmo se TYPESAFE_API_KEY estiver configurada")
    p.add_argument("--jev-min-probability", type=float, default=0.90,
                   help="Probabilidade mínima da escolha Jev em --jev-auto (default: 0.90)")
    p.add_argument("--jev-min-margin", type=float, default=0.20,
                   help="Margem mínima sobre a segunda opção em --jev-auto (default: 0.20)")
    args = p.parse_args()
    if not 0.0 <= args.jev_min_probability <= 1.0:
        p.error("--jev-min-probability precisa estar entre 0 e 1")
    if not 0.0 <= args.jev_min_margin <= 1.0:
        p.error("--jev-min-margin precisa estar entre 0 e 1")

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
        answers_path=args.answers,
        jev_shadow=args.jev_shadow,
        jev_auto=(
            args.jev_auto
            or (bool(os.getenv("TYPESAFE_API_KEY")) and not args.jev_shadow and not args.no_jev)
        ),
        jev_min_probability=args.jev_min_probability,
        jev_min_margin=args.jev_min_margin,
    )


if __name__ == "__main__":
    raise SystemExit(main())
