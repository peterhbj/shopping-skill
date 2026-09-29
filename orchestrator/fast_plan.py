"""Fast, read-only catalog planning for the local application."""

from __future__ import annotations

import hashlib
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from .browser import HELPERS_PATH, ProductResult, merge_sense_hits
from .decision.jev import JevAdvisor, JevError
from .enricher import enrich_list, load_profile, normalize
from .history import PurchaseHistory
from .main import _cand_payload, _jev_product_pass, _jev_profile_pass, _load_config, _plan_one, _planned_from_decision
from .selector import Ambiguity, Decision, packs_for_qty


_SIZE = re.compile(r"(?<!\w)(\d+(?:[,.]\d+)?)\s*(kg|gr?|l|lt|ml)\b", re.I)
_CACHE: dict[str, tuple[float, list[ProductResult]]] = {}
CACHE_SECONDS = 90


def package_size(name: str) -> tuple[str, float] | None:
    matches = list(_SIZE.finditer(name))
    if not matches:
        return None
    match = matches[-1]
    size = float(match.group(1).replace(",", "."))
    unit = match.group(2).lower()
    if unit in ("g", "gr"):
        return "kg", size / 1000
    if unit == "kg":
        return "kg", size
    if unit == "ml":
        return "l", size / 1000
    return "l", size


def _price_basis(result: ProductResult) -> ProductResult:
    size = package_size(result.name)
    if size and result.price_num > 0 and not result.sell_by_weight:
        result.price_base_dim = size[0]
        result.price_per_base_unit = result.price_num / size[1]
        if size[0] == "kg":
            result.weight_g = size[1] * 1000
        else:
            result.volume_ml = size[1] * 1000
    return result


async def _search_many_async(queries: dict[str, str], progress=None) -> tuple[dict[str, list[ProductResult]], dict[str, str]]:
    snapshots: dict[str, list[ProductResult]] = {}
    errors: dict[str, str] = {}
    to_search = {}
    for key, query in queries.items():
        cached = _CACHE.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            snapshots[key] = cached[1]
        else:
            to_search[key] = query
    if not to_search:
        return snapshots, errors
    semaphore = asyncio.Semaphore(4)
    script = HELPERS_PATH.read_text(encoding="utf-8")
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=os.getenv("ANDORINHA_BROWSER_PROFILE") or str(Path(tempfile.gettempdir()) / "andorinha-pw-profile"),
            headless=False, channel="msedge" if sys.platform == "win32" else None,
            viewport={"width": 1280, "height": 800},
        )
        await context.add_init_script(script=script)

        async def one(key: str, query: str, *, retry: bool = False) -> None:
            async with semaphore:
                page = await context.new_page()
                hits: list[dict] = []
                got_response = asyncio.Event()
                async def capture(response):
                    if "sense.osuper.com.br" not in response.url or "/search" not in response.url:
                        return
                    try:
                        hits.extend((await response.json()).get("hits") or [])
                        got_response.set()
                    except Exception:
                        pass
                page.on("response", capture)
                try:
                    from urllib.parse import quote
                    await page.goto("https://www.andorinhaonline.com.br/busca/" + quote(query, safe=""),
                                    wait_until="domcontentloaded", timeout=15000)
                    try:
                        await asyncio.wait_for(got_response.wait(), timeout=8 if retry else 6)
                    except asyncio.TimeoutError:
                        pass
                    try:
                        await page.locator(".item-product-wrapper").first.wait_for(timeout=1200)
                    except Exception:
                        pass
                    payload = await page.evaluate("() => window.andorinha_get_results ? andorinha_get_results() : null")
                    cards = [ProductResult.from_dict(row) for row in (payload or {}).get("results", [])]
                    results = merge_sense_hits(cards, hits)
                    snapshots[key] = [_price_basis(result) for result in results]
                    errors.pop(key, None)
                    if snapshots[key]:
                        _CACHE[key] = (time.monotonic(), snapshots[key])
                except Exception as exc:
                    snapshots[key] = []
                    errors[key] = str(exc)[:180]
                finally:
                    await page.close()
                    if progress:
                        progress({"stage": "search", "done": len(snapshots), "total": len(queries)})

        await asyncio.gather(*(one(key, query) for key, query in to_search.items()))
        for key, query in to_search.items():
            if not snapshots.get(key):
                await one(key, query, retry=True)
        await context.close()
    if errors and len(errors) == len(to_search):
        raise RuntimeError("Busca do Andorinha indisponível; nenhum resultado pôde ser verificado")
    return snapshots, errors


class _SearchSnapshot:
    def __init__(self, snapshots: dict[str, list[ProductResult]]):
        self.snapshots = snapshots
        self.query = ""

    def search(self, query: str) -> None:
        self.query = normalize(query)

    def get_results(self) -> list[ProductResult]:
        return self.snapshots.get(self.query, [])


def _request_qualifiers_ok(item: dict, candidate: ProductResult) -> bool:
    requested = normalize(" ".join(str(item.get(field) or "") for field in ("raw", "generic")))
    name = normalize(candidate.name)
    if item.get("lactose_free") and not any(x in name for x in ("sem lactose", "zero lactose", "s lactose", "s/lactose")):
        return False
    if "sem gas" in requested and "sem gas" not in name:
        return False
    if "com gas" in requested and "com gas" not in name and "c gas" not in name:
        return False
    flavor = normalize(str(item.get("flavor") or ""))
    if flavor and flavor not in name:
        return False
    brand = normalize(str(item.get("preferred_brand") or ""))
    # A brand named in this list is a hard request. A profile brand is a
    # preference and may lose to a verified equivalent with >=15% savings.
    if brand and brand in requested and brand not in name and brand not in normalize(str(candidate.brand_name or "")):
        return False
    return True


def _family_tokens(candidate: ProductResult) -> set[str]:
    name = normalize(_SIZE.sub(" ", candidate.name))
    brand = normalize(candidate.brand_name or "")
    ignored = {"com", "sem", "de", "da", "do", "tipo", "pct", "pacote", "embalagem", "caixa", "sache", "garrafa", "unidades", "unidade", "tradicional"}
    return {w for w in name.split() if w not in ignored and w not in set(brand.split()) and not w.isdigit()}


def _strict_equivalent(reference: ProductResult, candidate: ProductResult, item: dict) -> bool:
    if reference.product_id and reference.product_id == candidate.product_id:
        return True
    if not _request_qualifiers_ok(item, candidate):
        return False
    if reference.sale_unit != candidate.sale_unit or reference.price_base_dim != candidate.price_base_dim:
        return False
    reference_size, candidate_size = package_size(reference.name), package_size(candidate.name)
    if not reference_size or reference_size != candidate_size:
        return False
    ref_name, cand_name = normalize(reference.name), normalize(candidate.name)
    for marker in ("com gas", "sem gas", "sem lactose", "zero lactose"):
        if (marker in ref_name) != (marker in cand_name):
            return False
    return bool(_family_tokens(reference)) and _family_tokens(reference) == _family_tokens(candidate)


def _history_decision(item: dict, results: list[ProductResult], evidence: list[dict]) -> Decision | None:
    if not evidence:
        return None
    for prior in evidence:
        pid = str(prior.get("product_id") or "")
        name = normalize(str(prior.get("name") or ""))
        prior_result = next((r for r in results if pid and r.product_id == pid), None)
        if prior_result is None:
            prior_result = next((r for r in results if normalize(r.name) == name), None)
        if prior_result is None or not _request_qualifiers_ok(item, prior_result):
            continue
        chosen = prior_result
        rule = "history-repeat"
        for candidate in results:
            if candidate.index == prior_result.index or candidate.price_num <= 0:
                continue
            if not _strict_equivalent(prior_result, candidate, item):
                continue
            if candidate.price_per_base_unit <= 0 or prior_result.price_per_base_unit <= 0:
                continue
            if candidate.price_per_base_unit <= 0.85 * prior_result.price_per_base_unit:
                if chosen is prior_result or candidate.price_per_base_unit < chosen.price_per_base_unit:
                    chosen, rule = candidate, "history-equivalent-15pct"
        packs = packs_for_qty(item.get("qty") or 1, item.get("unit"))
        return Decision(index=chosen.index, name=chosen.name, price_num=chosen.price_num,
                        rule=rule, packs_needed=packs, total_cost=round(chosen.price_num * packs, 2),
                        notes=[f"Histórico: {prior.get('name')}; pedido atual prevalece sobre compras anteriores."])
    return None


def _merge_results(primary: list[ProductResult], additional: list[ProductResult]) -> list[ProductResult]:
    merged = []
    seen = set()
    for result in [*primary, *additional]:
        key = result.product_id or normalize(result.name)
        if key in seen:
            continue
        seen.add(key)
        merged.append(replace(result, index=len(merged)))
    return merged


def plan_fast(list_path: Path, profile_path: Path, history: PurchaseHistory,
              run_file: Path, *, jev_shadow: bool = True,
              progress=None) -> dict:
    started = time.monotonic()
    enriched = enrich_list(list_path, profile_path)
    items = enriched["items"]
    config, _ = _load_config(profile_path)
    profile = load_profile(profile_path)
    jev = None
    if jev_shadow and os.getenv("TYPESAFE_API_KEY"):
        try:
            jev = JevAdvisor()
            _jev_profile_pass(items, profile, jev, apply=False,
                              min_probability=0.90, min_margin=0.20)
        except JevError:
            jev = None
    evidences = []
    for item in items:
        evidence = history.related(str(item.get("generic") or item.get("item_text") or item.get("raw") or ""))
        for correction in history.related(str(item.get("raw") or "")):
            if correction.get("source") == "correction" and correction not in evidence:
                evidence.insert(0, correction)
        evidences.append(evidence)
    queries = {normalize(str(item.get("search_term") or "")): str(item.get("search_term") or "") for item in items}
    for evidence in evidences:
        if evidence and evidence[0].get("name"):
            historical_name = str(evidence[0]["name"])
            queries.setdefault(normalize(historical_name), historical_name)
    queries = {k: v for k, v in queries.items() if k}
    snapshots, search_errors = asyncio.run(_search_many_async(queries, progress))
    browser = _SearchSnapshot(snapshots)
    planned_list = []
    jev_queue: list[dict] = []
    for item, evidence in zip(items, evidences):
        planned = _plan_one(browser, item, config, jev_queue if jev else None)
        original_planned = planned
        search_key = normalize(str(item.get("search_term") or ""))
        if search_key in search_errors:
            planned["status"] = "needs_user"
            planned["notes"] = ["A busca falhou; tente gerar o plano novamente"]
            jev_queue = [entry for entry in jev_queue if entry["planned"] is not original_planned]
        # The current list and hard preferences must win over historical orders.
        historical_key = normalize(str(evidence[0].get("name") or "")) if evidence else ""
        results = _merge_results(snapshots.get(search_key, []),
                                 snapshots.get(historical_key, []) if historical_key != search_key else [])
        decision = None if search_key in search_errors else _history_decision(item, results, evidence)
        if decision:
            planned = _planned_from_decision(item, decision, "decided", results)
            jev_queue = [entry for entry in jev_queue if entry["planned"] is not original_planned]
        elif evidence and results and search_key not in search_errors:
            planned["status"] = "needs_user"
            planned["chosen_index"] = None
            planned["chosen_name"] = None
            planned["chosen_product_id"] = None
            planned["rule"] = "history-unavailable"
            planned["notes"] = ["Produto habitual não apareceu; escolha uma alternativa"]
            planned["candidates"] = _cand_payload(results)
            jev_queue = [entry for entry in jev_queue if entry["planned"] is not original_planned]
            alternatives = [r for r in results if _request_qualifiers_ok(item, r)]
            if jev and alternatives:
                jev_queue.append({"planned": planned, "item": item, "results": results,
                                  "ambiguity": Ambiguity(
                                      kind="usual_unavailable", item_raw=str(item.get("raw") or ""),
                                      reason="Produto habitual indisponível; sugerir alternativas compatíveis",
                                      candidates=alternatives[:5])})
        elif planned.get("rule") == "rotation+cheapest-per-base" and len(planned.get("candidates") or []) > 1:
            planned["status"] = "needs_user"
            planned["notes"] = ["Sem histórico confirmado: revisar a escolha por preço"]
        planned["history_evidence"] = evidence
        planned_list.append(planned)
    if jev and jev_queue:
        _jev_product_pass(jev_queue, jev, config, apply=False,
                          min_probability=0.90, min_margin=0.20)
    source_bytes = list_path.read_bytes()
    meta = {**enriched.get("meta", {}), "created_at": datetime.now().isoformat(timespec="seconds"),
            "list_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "history": history.counts(), "elapsed_s": round(time.monotonic() - started, 2),
            "jev_mode": "shadow" if jev else "disabled"}
    payload = {"meta": meta, "items": planned_list}
    run_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if progress:
        progress({"stage": "complete", "done": len(items), "total": len(items)})
    return payload
