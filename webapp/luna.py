"""
luna.py — passe em lote do GPT-6 Luna (via Codex CLI) sobre as dúvidas do run.json.

Uma chamada de modelo por corrida, não por item. Só escolhe candidatos que já
estão nos resultados da busca; o que ficar abaixo do limiar volta para a pessoa.
"""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.llm.adapter import LLMAdapter, LLMCallError

MIN_CONFIDENCE = 0.85
MAX_CANDIDATES = 8

PROMPT = """Você ajuda uma família a montar a compra do supermercado Andorinha.
Para cada item abaixo, escolha UM candidato da lista de resultados da busca, ou
devolva index null quando a escolha depender do gosto da pessoa (sabor, marca
nova, tamanho muito diferente do pedido, restrição alimentar incerta).

Regras:
- Respeite o pedido da lista (quantidade, sabor, sem lactose, marca citada).
- Siga as preferências da família abaixo.
- Prefira o produto equivalente com melhor preço por unidade base, salvo
  quando a marca preferida é explicitamente pedida.
- Nunca invente um índice fora dos candidatos.
- confianca entre 0 e 1: use >= 0.85 só quando tiver certeza de que a família
  aceitaria sem reclamar.

Preferências da família (YAML):
{profile}

Itens com dúvida (JSON):
{items}

Responda APENAS com JSON no formato:
{{"decisoes": [{{"id": 0, "index": 2, "confianca": 0.9, "motivo": "frase curta em português"}}]}}
"""


def pending_items(run: dict) -> list[dict]:
    return [it for it in run.get("items") or [] if it.get("status") == "needs_user"]


def build_prompt(run: dict, profile_text: str) -> str:
    payload = []
    for n, it in enumerate(pending_items(run)):
        payload.append({
            "id": n,
            "pedido": it.get("raw"),
            "qty": it.get("qty"),
            "unit": it.get("unit"),
            "duvida": " ".join(it.get("notes") or [])[:300],
            "candidatos": [
                {
                    "index": c.get("index"),
                    "nome": c.get("name"),
                    "preco": c.get("price_num"),
                    "preco_por_base": c.get("price_per_base_unit"),
                    "base": c.get("price_base_dim"),
                }
                for c in (it.get("candidates") or [])[:MAX_CANDIDATES]
            ],
            "sugestao_jev": it.get("jev"),
        })
    return PROMPT.format(
        profile=profile_text[:8000],
        items=json.dumps(payload, ensure_ascii=False),
    )


def apply_decisions(run: dict, reply: dict, min_confidence: float = MIN_CONFIDENCE) -> dict[str, str]:
    """Anota `luna` em cada dúvida e devolve {raw: índice} das escolhas confiantes."""
    pend = pending_items(run)
    confident: dict[str, str] = {}
    for d in (reply or {}).get("decisoes") or []:
        try:
            item = pend[int(d.get("id"))]
        except (TypeError, ValueError, IndexError):
            continue
        idx = d.get("index")
        valid = {c.get("index") for c in item.get("candidates") or []}
        if not isinstance(idx, int) or idx not in valid:
            idx = None
        try:
            conf = float(d.get("confianca") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        item["luna"] = {"index": idx, "confianca": conf, "motivo": str(d.get("motivo") or "")[:200]}
        if idx is not None and conf >= min_confidence:
            confident[str(item.get("raw"))] = str(idx)
    return confident


def run_luna(run_path: Path, profile_path: Path, adapter: LLMAdapter, log=print) -> dict[str, str]:
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if not pending_items(run):
        log("[luna] nenhuma dúvida para revisar")
        return {}
    log(f"[luna] revisando {len(pending_items(run))} dúvida(s) em lote…")
    try:
        reply = adapter.ask_json(build_prompt(run, profile_path.read_text(encoding="utf-8")))
    except LLMCallError as e:
        log(f"[luna] falhou, as dúvidas vão para você: {e}")
        return {}
    confident = apply_decisions(run, reply)
    run_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[luna] resolveu {len(confident)} com confiança; o resto fica para você")
    return confident
