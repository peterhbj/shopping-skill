"""
luna.py — passe em lote do GPT-6 Luna (via Codex CLI) sobre o run.json.

Uma chamada de modelo por corrida, não por item, com três tarefas:
- dúvidas: escolher entre os candidatos das que ficaram para a pessoa;
- conferência: revisar escolhas automáticas por regra (menor preço, rotação),
  que podem pegar o produto errado (água sem gás quando a lista pede com gás);
- não encontrados: sugerir outro nome de busca para o jeito de escrever da casa.

Só escolhe candidatos que já estão nos resultados da busca; o que ficar abaixo
do limiar volta para a pessoa.
"""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.llm.adapter import LLMAdapter, LLMCallError

MIN_CONFIDENCE = 0.85
MAX_CANDIDATES = 8
# Escolhas que a pessoa ou um modelo já fizeram, ou marca fixa do perfil.
TRUSTED_RULES = ("preferred-brand",)
TRUSTED_PREFIXES = ("user-", "llm-")

PROMPT = """Você ajuda uma família a montar a compra do supermercado Andorinha.
A lista é escrita à mão por quem cuida da casa: tem apelidos, erros de
digitação e abreviações. Responda as três tarefas abaixo.

Regras gerais:
- Respeite o pedido (quantidade, sabor, com/sem gás, sem lactose, marca citada).
- Siga as preferências da família abaixo.
- Nunca invente um índice fora dos candidatos de cada item.
- confianca entre 0 e 1: use >= 0.85 só quando tiver certeza de que a família
  aceitaria sem reclamar.

1. "duvidas": para cada item, escolha UM candidato ou devolva index null quando
   depender do gosto da pessoa (sabor, marca nova, tamanho muito diferente,
   restrição alimentar incerta). Prefira o equivalente com melhor preço por
   unidade base, salvo marca preferida explícita.

2. "conferencia": o sistema escolheu sozinho por regra de preço. Marque
   ok=false só quando o produto escolhido claramente NÃO é o que foi pedido
   (outro tipo de produto, com gás x sem gás, sabor ou variedade errada,
   tamanho absurdo para a quantidade). Nesse caso indique o candidato certo em
   index, ou null se nenhum servir. Diferença só de marca ou preço não é erro.

3. "buscas": nenhum produto foi encontrado. Diga como esse item provavelmente
   se chama no site de um supermercado (corrija erro de digitação e apelido,
   ex.: "Nescan Ball" → "Nescau Ball"). O termo vai na busca do site, que
   casa palavra por palavra: use 1 a 3 palavras do nome do produto, sem
   tamanho, volume ou quantidade ("água sanitária", não "água sanitária 2
   litros"). Use termo null se não souber.

Preferências da família (YAML):
{profile}

Dados (JSON):
{data}

Responda APENAS com JSON no formato:
{{"duvidas": [{{"id": 0, "index": 2, "confianca": 0.9, "motivo": "frase curta"}}],
 "conferencia": [{{"id": 0, "ok": true, "index": null, "confianca": 0.9, "motivo": "frase curta"}}],
 "buscas": [{{"id": 0, "termo": "nome no site", "motivo": "frase curta"}}]}}
"""


def pending_items(run: dict) -> list[dict]:
    return [it for it in run.get("items") or [] if it.get("status") == "needs_user"]


def auto_decided_items(run: dict) -> list[dict]:
    out = []
    for it in run.get("items") or []:
        rule = str(it.get("rule") or "")
        if it.get("status") != "decided" or not it.get("candidates"):
            continue
        if rule in TRUSTED_RULES or rule.startswith(TRUSTED_PREFIXES):
            continue
        out.append(it)
    return out


def missing_items(run: dict) -> list[dict]:
    return [it for it in run.get("items") or [] if it.get("status") == "not_found"]


def _candidates(it: dict) -> list[dict]:
    return [
        {
            "index": c.get("index"),
            "nome": c.get("name"),
            "preco": c.get("price_num"),
            "preco_por_base": c.get("price_per_base_unit"),
            "base": c.get("price_base_dim"),
        }
        for c in (it.get("candidates") or [])[:MAX_CANDIDATES]
    ]


def build_prompt(run: dict, profile_text: str) -> str:
    data = {
        "duvidas": [
            {
                "id": n, "pedido": it.get("raw"), "qty": it.get("qty"), "unit": it.get("unit"),
                "duvida": " ".join(it.get("notes") or [])[:300],
                "candidatos": _candidates(it), "sugestao_jev": it.get("jev"),
            }
            for n, it in enumerate(pending_items(run))
        ],
        "conferencia": [
            {
                "id": n, "pedido": it.get("raw"), "qty": it.get("qty"),
                "escolhido": {"index": it.get("chosen_index"), "nome": it.get("chosen_name")},
                "regra": it.get("rule"), "candidatos": _candidates(it),
            }
            for n, it in enumerate(auto_decided_items(run))
        ],
        "buscas": [
            {"id": n, "pedido": it.get("raw"), "busca_feita": it.get("search_term")}
            for n, it in enumerate(missing_items(run))
        ],
    }
    return PROMPT.format(profile=profile_text[:8000], data=json.dumps(data, ensure_ascii=False))


def _row(rows: list[dict], d: dict) -> dict | None:
    try:
        return rows[int(d.get("id"))]
    except (TypeError, ValueError, IndexError):
        return None


def _conf(d: dict) -> float:
    try:
        return float(d.get("confianca") or 0)
    except (TypeError, ValueError):
        return 0.0


def _valid_index(item: dict, idx) -> int | None:
    valid = {c.get("index") for c in item.get("candidates") or []}
    return idx if isinstance(idx, int) and idx in valid else None


def apply_decisions(run: dict, reply: dict, min_confidence: float = MIN_CONFIDENCE) -> dict[str, str]:
    """Anota `luna` nos itens e devolve {raw: índice} das escolhas confiantes."""
    reply = reply or {}
    # Listas montadas antes de mexer em status: os ids se referem a elas.
    pend, auto, missing = pending_items(run), auto_decided_items(run), missing_items(run)
    confident: dict[str, str] = {}

    for d in reply.get("duvidas") or reply.get("decisoes") or []:
        item = _row(pend, d)
        if item is None:
            continue
        idx, conf = _valid_index(item, d.get("index")), _conf(d)
        item["luna"] = {"index": idx, "confianca": conf, "motivo": str(d.get("motivo") or "")[:200]}
        if idx is not None and conf >= min_confidence:
            confident[str(item.get("raw"))] = str(idx)

    for d in reply.get("conferencia") or []:
        item = _row(auto, d)
        if item is None or d.get("ok") is not False:
            continue
        idx, conf = _valid_index(item, d.get("index")), _conf(d)
        if idx == item.get("chosen_index"):
            continue
        motivo = str(d.get("motivo") or "")[:200]
        item["luna"] = {"index": idx, "confianca": conf, "motivo": motivo, "conferencia": True}
        item["notes"] = list(item.get("notes") or []) + [
            f"escolha automática era: {item.get('chosen_name')}", f"Luna: {motivo}",
        ]
        item.update({"status": "needs_user", "chosen_index": None, "chosen_name": None,
                     "packs_needed": 0, "total_cost": 0, "rule": None})
        if idx is not None and conf >= min_confidence:
            confident[str(item.get("raw"))] = str(idx)

    for d in reply.get("buscas") or []:
        item = _row(missing, d)
        termo = str(d.get("termo") or "").strip()
        if item is None or not termo or termo.lower() == str(item.get("search_term") or "").lower():
            continue
        item["luna"] = {"termo": termo[:80], "motivo": str(d.get("motivo") or "")[:200]}

    return confident


def run_luna(run_path: Path, profile_path: Path, adapter: LLMAdapter, log=print) -> dict[str, str]:
    run = json.loads(run_path.read_text(encoding="utf-8"))
    pend, auto, missing = pending_items(run), auto_decided_items(run), missing_items(run)
    if not (pend or auto or missing):
        log("[luna] nada para revisar")
        return {}
    log(f"[luna] revisando {len(pend)} dúvida(s), conferindo {len(auto)} escolha(s) "
        f"automática(s) e {len(missing)} item(ns) não encontrado(s)…")
    try:
        reply = adapter.ask_json(build_prompt(run, profile_path.read_text(encoding="utf-8")))
    except LLMCallError as e:
        log(f"[luna] falhou, as dúvidas vão para você: {e}")
        return {}
    confident = apply_decisions(run, reply)
    run_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    flagged = sum(1 for it in run["items"] if (it.get("luna") or {}).get("conferencia"))
    renamed = sum(1 for it in run["items"] if (it.get("luna") or {}).get("termo"))
    log(f"[luna] {len(confident)} escolha(s) com confiança; {flagged} escolha(s) automática(s) "
        f"corrigida(s); {renamed} nome(s) sugerido(s) para busca")
    return confident
