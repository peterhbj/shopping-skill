"""
selector.py — Regras determinísticas de seleção de produto.

Recebe: item enriquecido (do enricher) + list[ProductResult] (do browser).
Retorna: Decision (escolha concreta) ou Ambiguity (precisa LLM).

Regras em ordem:
  1. Filtro lactose (se household lactose_free + categoria láctea → exige SL)
  2. Pack optimization (problema 1): se há múltiplas embalagens com
     diferenças significativas de price_per_base_unit, escolhe a melhor.
  3. Preferred brand match (KB):
     - exato: decisão direta
     - match parcial + sem alternativa significativamente melhor: decisão
     - match parcial + alternativa ≥ threshold% mais barata/base: Ambiguity
  4. Rotation: menor price_per_base_unit
  5. Fallback: primeiro resultado relevante

Quando retorna Ambiguity, o caller pode passar a um LLMAdapter.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

from .browser import ProductResult


# =============================================================================
# Tipos de saída
# =============================================================================


@dataclass
class Decision:
    """Escolha determinística do produto."""
    index: int
    name: str
    price_num: float
    rule: str
    packs_needed: int = 1
    total_cost: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass
class Ambiguity:
    """Sinaliza necessidade de decisão humana ou LLM."""
    kind: str  # "dominance" | "no_match" | "lactose_unavailable"
    item_raw: str
    reason: str
    candidates: list[ProductResult]
    preferred: Optional[ProductResult] = None


@dataclass
class NoResult:
    """Nenhum produto adequado encontrado."""
    item_raw: str
    reason: str


SelectorOutcome = Decision | Ambiguity | NoResult


# =============================================================================
# Filtros
# =============================================================================


_LACTOSE_INDICATORS = (
    "s/lactose",
    "s/ lactose",
    "sem lactose",
    "zero lactose",
    "0 lactose",
    " sl ",
    " sl-",
    "-sl ",
    "s.lactose",
    "lacfree",
    "lactose free",
)

_DAIRY_KEYWORDS = (
    "leite",
    "queijo",
    "iogurte",
    "manteiga",
    "requeijão",
    "requeijao",
    "creme de leite",
    "leite condensado",
)


def is_dairy_category(item_text: str, category: str | None) -> bool:
    text = f"{item_text} {category or ''}".lower()
    # Queijo ralado NÃO exige SL (produto praticamente inexistente sem lactose)
    if "ralado" in text:
        return False
    return any(kw in text for kw in _DAIRY_KEYWORDS)


def is_lactose_free_label(name_lower: str) -> bool:
    return any(ind in name_lower for ind in _LACTOSE_INDICATORS)


def apply_lactose_filter(
    candidates: list[ProductResult],
    household_lactose_free: bool,
    item_text: str,
    category: str | None,
) -> tuple[list[ProductResult], bool]:
    """
    Retorna (filtered, was_filtered). Se household exige SL e item é lácteo,
    remove candidatos sem indicação de SL.
    """
    if not household_lactose_free:
        return candidates, False
    if not is_dairy_category(item_text, category):
        return candidates, False
    filtered = [c for c in candidates if is_lactose_free_label(c.name_lower)]
    return filtered, True


# =============================================================================
# Matching de marca preferida
# =============================================================================


# Tokens genéricos demais para matching de marca (causam falsos positivos)
_WEAK_BRAND_TOKENS = frozenset({
    "leve", "pague", "pacote", "caixa", "lata", "rolo", "rolos", "unid",
    "original", "tradicional", "integral", "light", "zero", "natural",
    "extra", "fino", "fina", "grande", "pequeno", "medio", "médio",
    "folha", "dupla", "tripla", "neutro", "oferta", "economica", "econômica",
    "embalagem", "refil", "garrafa", "pote", "bandeja", "sache", "sachê",
    "vácuo", "vacuo", "tipo", "com", "sem", "para", "cada", "dia",
    "prata", "ouro", "pedaço", "pedaco", "fatiado", "granel",
})


def match_preferred_brand(
    candidates: list[ProductResult],
    preferred_brand: str | None,
) -> list[ProductResult]:
    """
    Retorna candidatos que matcham a marca preferida (substring chave).
    Ordenado por número de tokens em comum (descrescente).

    Ignora tokens numéricos/dimensionais e tokens genéricos fracos
    (leve, pague, tradicional, prata, etc.) que geravam matches errados.
    """
    if not preferred_brand:
        return []
    pb_lower = preferred_brand.lower()
    raw_tokens = [t for t in re.split(r"\W+", pb_lower) if len(t) >= 3]
    pb_tokens = [
        t for t in raw_tokens
        if not re.search(r"\d", t)
        and not re.fullmatch(r"(?:ml|kg|gramas?|litros?|un|unid|pacote|caixa|lata|cx|pct|gr?)", t)
        and t not in _WEAK_BRAND_TOKENS
    ]
    if not pb_tokens:
        return []
    # Exige pelo menos 1 token "forte"; se houver 2+, exige metade
    min_match = max(1, (len(pb_tokens) + 1) // 2)

    scored: list[tuple[int, ProductResult]] = []
    for c in candidates:
        overlap = sum(1 for tok in pb_tokens if tok in c.name_lower)
        if overlap >= min_match:
            scored.append((overlap, c))
    if not scored:
        return []
    scored.sort(key=lambda x: -x[0])
    top_overlap = scored[0][0]
    return [c for ov, c in scored if ov == top_overlap]


# =============================================================================
# Pack optimization (problema 1)
# =============================================================================


def optimize_pack(
    candidates: list[ProductResult],
    target_qty: int,
    max_packs: int = 6,
) -> Optional[tuple[ProductResult, int, float]]:
    """
    Para itens onde a "qty" significa unidades atômicas (ex: rolos de papel
    higiênico, sachês de molho), encontra a combinação (produto, packs_needed)
    com menor custo total para atingir target_qty unidades.

    Retorna (best_product, packs_needed, total_cost) ou None se nenhum
    candidato tem unit_count > 1.
    """
    # Considerar apenas candidatos com unit_count válido (>1)
    eligible = [c for c in candidates if c.unit_count and c.unit_count > 1]
    if not eligible:
        return None

    best: Optional[tuple[ProductResult, int, float]] = None
    for c in eligible:
        packs = math.ceil(target_qty / c.unit_count)
        if packs > max_packs:
            continue
        total = packs * c.price_num
        if best is None or total < best[2]:
            best = (c, packs, total)
    return best


# =============================================================================
# Detecção de dominância de oferta (problema 4)
# =============================================================================


def evaluate_dominance(
    preferred_candidates: list[ProductResult],
    all_candidates: list[ProductResult],
    threshold_pct: float,
) -> Optional[list[ProductResult]]:
    """
    Se algum candidato NÃO-preferred tem price_per_base_unit
    >= threshold_pct% menor que o preferred TOP (maior overlap, já ordenado),
    retorna lista desses candidatos. Caso contrário, None (sem ambiguidade).

    Nota: usa preferred_candidates[0] (top match), não o mais barato.
    Isso evita falso negativo quando match_preferred captura múltiplos
    candidatos por tokens genéricos (ex: "Vácuo" em café).
    """
    if not preferred_candidates:
        return None
    preferred_top = preferred_candidates[0]
    pb_dim = preferred_top.price_base_dim
    if preferred_top.price_per_base_unit <= 0:
        return None
    threshold = preferred_top.price_per_base_unit * (1 - threshold_pct / 100)

    preferred_indices = {p.index for p in preferred_candidates}
    cheaper = [
        c
        for c in all_candidates
        if c.index not in preferred_indices
        and c.price_base_dim == pb_dim
        and c.price_per_base_unit > 0
        and c.price_per_base_unit < threshold
    ]
    return cheaper or None


# =============================================================================
# Decisão principal
# =============================================================================


@dataclass
class SelectorConfig:
    dominance_threshold_pct: float = 15.0
    pack_optimization: bool = True
    max_packs_per_item: int = 6
    household_lactose_free: bool = False


def _is_weight_unit(unit: str | None) -> bool:
    return (unit or "").lower().strip() in ("g", "kg", "grama", "gramas")


# qty nessas unidades = número de unidades atômicas (pode usar unit_count).
# "pacotes" / "garrafas" / "kits" = número de produtos a clicar; NÃO dividir por C/24.
_ATOMIC_QTY_UNITS = frozenset({
    "un", "unidade", "unidades",
    "rolos", "rolo",
    "sache", "sachê", "saches", "sachês",
})


def _qty_is_atomic(unit: str | None) -> bool:
    return (unit or "un").lower().strip() in _ATOMIC_QTY_UNITS


def _packs_needed_for_item(
    enriched_item: dict,
    chosen: ProductResult,
    target_qty: int | float,
    pack_opt: bool,
    max_packs: int,
) -> tuple[int, float]:
    """
    Converte qty do KB/lista em número de cliques no carrinho.

    - unit g/kg → sempre 1 pack (peso se ajusta no site / é granel)
    - unit_count > 1 + pack_opt → ceil(qty / unit_count)
    - caso contrário → max(1, int(qty)) limitado
    """
    unit = (enriched_item.get("unit") or "").lower()
    if _is_weight_unit(unit) or chosen.price_per_kg:
        return 1, round(chosen.price_num, 2)

    if (
        pack_opt
        and _qty_is_atomic(unit)
        and chosen.unit_count
        and chosen.unit_count > 1
        and isinstance(target_qty, (int, float))
    ):
        packs = max(1, math.ceil(float(target_qty) / chosen.unit_count))
        packs = min(packs, max_packs)
    else:
        # qty explícita da lista (leite 12, água 12, molho 10, 2 pacotes).
        # Não aplicar max_packs — isso cortava 12→6.
        packs = max(1, int(target_qty) if isinstance(target_qty, (int, float)) else 1)

    return packs, round(packs * chosen.price_num, 2)


_CORE_PRODUCT_WORDS = frozenset({
    "cafe", "café", "arroz", "feijao", "feijão", "sucrilhos", "nescau", "farofa",
    "molho", "tomate", "maionese", "ketchup", "mostarda", "azeite", "vinagre",
    "leite", "condensado", "creme", "requeijao", "requeijão", "queijo", "ralado",
    "atum", "papel", "higienico", "higiênico", "toalha", "detergente",
    "pipoca", "biscoito", "bolacha", "maizena", "torrada", "pao", "pão",
    "bisnaguinha", "agua", "água", "refrigerante", "linguiça", "linguica",
    "nescafe", "nescafé", "soluvel", "solúvel",
    "bacon", "frango", "peito", "coxa", "carne", "filtro", "cereal",
})


def _filter_relevant(
    candidates: list[ProductResult],
    enriched_item: dict,
) -> list[ProductResult]:
    """
    Remove resultados cujo nome não combina com o item pedido.
    Exige palavra-núcleo; rejeita pares clássicos de confusão.
    """
    generic = (enriched_item.get("generic") or "").lower()
    item_text = (enriched_item.get("item_text") or enriched_item.get("raw") or "").lower()
    category = (enriched_item.get("category") or "").lower()

    stop = {
        "de", "da", "do", "com", "sem", "para", "em", "e", "ou", "a", "o", "um", "uma",
        "tipo", "pacote", "caixa", "garrafa", "pote",
    }
    keywords: set[str] = set()
    for src in (generic, item_text):
        for tok in re.split(r"\W+", src):
            if len(tok) >= 3 and tok not in stop and not tok.isdigit():
                keywords.add(tok)

    core = keywords & _CORE_PRODUCT_WORDS
    for tok in re.split(r"\W+", generic):
        if len(tok) >= 4 and tok not in stop:
            core.add(tok)

    # Sinônimos: o site usa um termo, a lista usa outro
    _SYNONYMS = {
        "bolacha": {"biscoito", "maizena", "rosquinha", "rosquinhas"},
        "biscoito": {"bolacha", "maizena"},
        "pao": {"bisnaguinha", "pão", "pao"},
        "pão": {"bisnaguinha", "pao"},
        "file": {"peito", "filé", "file"},
        "filé": {"peito", "file"},
        "refri": {"refrigerante", "bioleve"},
        "agua": {"água", "mineral"},
        "água": {"agua", "mineral"},
    }
    expanded_core = set(core)
    for k in list(core | keywords):
        for syn in _SYNONYMS.get(k, ()):
            expanded_core.add(syn)
    core = expanded_core

    reject_if: list[str] = []
    # Só rejeita "molho" quando o pedido é tomate in natura — NÃO quando o pedido É molho
    _wants_sauce = any(k in keywords for k in ("molho", "extrato", "polpa", "passata"))
    _is_produce = category == "hortifruti" or any(
        k in keywords for k in ("tomate", "cebola", "alface", "pepino")
    )
    if _is_produce:
        reject_if += ["semente", "sementes", "muda"]
    if not _wants_sauce and _is_produce:
        reject_if += ["molho", "extrato", "catchup", "ketchup", "polpa", "passata", "conserva", "maionese"]
    if "arroz" in keywords:
        reject_if += ["feijao", "feijão", "filtro"]
    if any(k in keywords for k in ("cafe", "café")) and "filtro" not in keywords:
        reject_if += ["filtro"]
    if "farofa" in keywords:
        reject_if += ["pipoca", "milho"]
    if "molho" in keywords:
        reject_if += ["maionese", "mostarda", "ketchup", "catchup"]
    if "condensado" in keywords:
        reject_if += ["creme"]
    if "creme" in keywords and "condensado" not in keywords:
        reject_if += ["condensado"]
    if any(k in keywords for k in ("sucrilhos", "nescau", "cereal")):
        reject_if += ["papel", "higienico", "higiênico"]
    if any(k in keywords for k in ("coala", "orquidea", "orquídea")):
        reject_if += ["lava roupa", "lava-roupa", "lavaroupas", "amaciante"]

    filtered: list[ProductResult] = []
    for c in candidates:
        name = c.name_lower
        if reject_if and any(r in name for r in reject_if):
            continue
        joined = f"{generic} {item_text}"

        # Água com gás
        if any(x in joined for x in ("gás", "gas")):
            if "sem gas" in name or "sem gás" in name:
                continue
        # Queijo ralado: não kg
        if "ralado" in keywords and re.search(r"\bkg\b", name) and not re.search(r"\d+\s*g\b", name):
            continue
        # Leite líquido: não pó
        if "leite" in keywords and not any(x in keywords for x in ("po", "pó", "condensado", "creme")):
            if "em po" in name or "em pó" in name or "leite po" in name or "leite pó" in name:
                continue
        # CONDENSADO: nome deve ter a palavra
        if "condensado" in keywords and "condensado" not in name:
            continue
        # Pipoca microondas: rejeitar milho de pipoca
        if "pipoca" in keywords or "microondas" in generic:
            if "milho" in name and "microondas" not in name and "micro" not in name:
                continue
        # Peito/filé: rejeitar c/osso quando pedimos sem osso
        if any(k in joined for k in ("peito", "filé", "file", "frango")):
            wants_boneless = any(x in joined for x in ("sem osso", "s/osso", "s/ osso", "s osso", "desossado"))
            # default da lista "filé de frango" / peito → preferir s/osso
            if wants_boneless or "peito" in joined:
                if any(x in name for x in ("c/osso", "com osso", "c/ osso", "c osso")):
                    continue
        # Molho: nome deve conter molho
        if "molho" in keywords and "molho" not in name:
            continue

        # Café solúvel / Nescafé: não pegar "com leite" a menos que a lista peça
        wants_com_leite = any(x in joined for x in ("com leite", "c/leite", "c/ leite"))
        if (
            any(k in keywords for k in ("nescafe", "nescafé"))
            or "soluvel" in generic
            or "solúvel" in generic
        ):
            if not wants_com_leite and any(
                x in name for x in ("com leite", "c/leite", "c/ leite", "c leite")
            ):
                continue

        # Coxa e sobrecoxa: exige os dois cortes (não só coxa — "coxa" ⊂ "sobrecoxa")
        if "coxa" in keywords and "sobrecoxa" in keywords:
            if not (re.search(r"\bcoxa\b", name) and re.search(r"\bsobrecoxa\b", name)):
                continue

        # "pedaço" ≠ fatiado
        if any(x in joined for x in ("pedaço", "pedaco")) and any(
            x in name for x in ("fatiado", "fatiada")
        ):
            continue

        # Esponja: amarela/multiuso ≠ azul "não risca"
        if any(x in joined for x in ("não risca", "nao risca", "n/risca")):
            if not any(x in name for x in ("não risca", "nao risca", "n/risca", "nao-risca")):
                if "azul" not in name:
                    continue
        elif any(x in joined for x in ("amarela", "multiuso")):
            if any(x in name for x in ("não risca", "nao risca", "n/risca")):
                continue

        # Bisteca/pernil em kg: rejeita pacote congelado pequeno (800g pct)
        item_unit = (enriched_item.get("unit") or "").lower()
        if (
            item_unit in ("kg", "g")
            and any(k in keywords for k in ("bisteca", "pernil", "copa"))
            and re.search(r"cong", name)
            and re.search(r"\d+\s*g\b", name)
            and not re.search(r"\bkg\b", name)
        ):
            continue

        if keywords and not any(k in name for k in keywords):
            continue
        if core and not any(k in name for k in core):
            continue
        filtered.append(c)

    # Refri Bioleve: se tem 1,5L na lista, descarta 510ml
    if any(k in keywords for k in ("bioleve", "refrigerante", "refri")) and len(filtered) > 1:
        large = [c for c in filtered if re.search(r"1[.,]?5\s*(?:l|lt|litro)", c.name_lower)]
        if large:
            filtered = large

    return filtered


def select(
    enriched_item: dict,
    candidates: list[ProductResult],
    config: SelectorConfig,
) -> SelectorOutcome:
    """
    Aplica as regras em ordem e retorna Decision, Ambiguity ou NoResult.
    """
    raw = enriched_item.get("raw", "")
    item_text = enriched_item.get("item_text") or raw
    raw_qty = enriched_item.get("qty") or 1
    try:
        target_qty: int | float = float(raw_qty)
        if target_qty == int(target_qty):
            target_qty = int(target_qty)
    except (TypeError, ValueError):
        target_qty = 1
    preferred_brand = enriched_item.get("preferred_brand")
    rotation = bool(enriched_item.get("rotation"))
    item_lactose_free = bool(enriched_item.get("lactose_free"))
    category = enriched_item.get("category")
    unit = (enriched_item.get("unit") or "un").lower()

    if not candidates:
        return NoResult(item_raw=raw, reason="Nenhum resultado da busca")

    # ----- 0) Filtro de relevância (nome deve combinar com o item)
    candidates = _filter_relevant(candidates, enriched_item)
    if not candidates:
        return NoResult(
            item_raw=raw,
            reason="Nenhum resultado passou no filtro de relevância (nome não combina com o item)",
        )

    # ----- 1) Filtro de lactose
    needs_sl = config.household_lactose_free or item_lactose_free
    if needs_sl and is_dairy_category(item_text, category):
        sl_only, was_filtered = apply_lactose_filter(
            candidates, household_lactose_free=True, item_text=item_text, category=category
        )
        if not sl_only:
            return NoResult(
                item_raw=raw,
                reason="Household exige S/Lactose mas nenhum candidato SL encontrado",
            )
        candidates = sl_only

    # ----- 2) Pack optimization (só para unidades contáveis, nunca g/kg)
    pack_result = None
    if (
        config.pack_optimization
        and not _is_weight_unit(unit)
        and _qty_is_atomic(unit)
        and isinstance(target_qty, int)
        and target_qty > 1
        and candidates
    ):
        pack_result = optimize_pack(candidates, target_qty, config.max_packs_per_item)

    # ----- 3) Match de marca preferida
    preferred = match_preferred_brand(candidates, preferred_brand) if preferred_brand and not rotation else []

    # ----- 3a) Avaliar dominância de oferta entre preferred vs alternativas
    dominance_alternatives = None
    if preferred:
        dominance_alternatives = evaluate_dominance(
            preferred_candidates=preferred,
            all_candidates=candidates,
            threshold_pct=config.dominance_threshold_pct,
        )

    if dominance_alternatives:
        return Ambiguity(
            kind="dominance",
            item_raw=raw,
            reason=(
                f"Marca preferida ({preferred[0].name}) tem alternativa(s) "
                f"≥{config.dominance_threshold_pct:.0f}% mais barata/base — "
                f"vale revisar?"
            ),
            candidates=([preferred[0]] + dominance_alternatives)[:5],
            preferred=preferred[0],
        )

    # ----- 3b) Pack optimization
    if pack_result:
        chosen_pack, packs_needed, total_cost = pack_result
        in_preferred = preferred and chosen_pack.index in {p.index for p in preferred}
        rule_parts = ["pack-optimized"]
        if in_preferred:
            rule_parts.append("preferred-brand")
        elif preferred:
            rule_parts.append("dominância-de-preço")
        return Decision(
            index=chosen_pack.index,
            name=chosen_pack.name,
            price_num=chosen_pack.price_num,
            rule="+".join(rule_parts),
            packs_needed=packs_needed,
            total_cost=round(total_cost, 2),
            notes=[
                f"{packs_needed} pacote(s) × R${chosen_pack.price_num:.2f} "
                f"= R${total_cost:.2f} (R${chosen_pack.price_per_base_unit:.4f}/{chosen_pack.price_base_dim})"
            ],
        )

    if preferred:
        chosen = preferred[0]
        packs, total = _packs_needed_for_item(
            enriched_item, chosen, target_qty, config.pack_optimization, config.max_packs_per_item
        )
        return Decision(
            index=chosen.index,
            name=chosen.name,
            price_num=chosen.price_num,
            rule="preferred-brand",
            packs_needed=packs,
            total_cost=total,
        )

    # ----- 4) Rotation: menor price_per_base_unit
    if not candidates:
        return NoResult(item_raw=raw, reason="Sem candidatos após filtros")

    if rotation or not preferred_brand:
        chosen = min(
            candidates,
            key=lambda c: c.price_per_base_unit if c.price_per_base_unit > 0 else c.price_num,
        )
        packs, total = _packs_needed_for_item(
            enriched_item, chosen, target_qty, config.pack_optimization, config.max_packs_per_item
        )
        return Decision(
            index=chosen.index,
            name=chosen.name,
            price_num=chosen.price_num,
            rule="rotation+cheapest-per-base",
            packs_needed=packs,
            total_cost=total,
        )

    # ----- 5) Fallback
    chosen = candidates[0]
    packs, total = _packs_needed_for_item(
        enriched_item, chosen, target_qty, config.pack_optimization, config.max_packs_per_item
    )
    return Decision(
        index=chosen.index,
        name=chosen.name,
        price_num=chosen.price_num,
        rule="first-result-fallback",
        packs_needed=packs,
        total_cost=total,
    )
