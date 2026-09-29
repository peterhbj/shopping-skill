"""
selector.py — Regras determinísticas de seleção de produto.

Recebe: item enriquecido (do enricher) + list[ProductResult] (do browser).
Retorna: Decision (só se estiver claro) ou Ambiguity/NoResult (perguntar ao usuário).

Regras em ordem:
  1. Filtro lactose (se household lactose_free + categoria lácteo → exige SL)
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
from .enricher import normalize


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
    kind: str  # "dominance" | "missing_brand" | "unsure" | "no_match"
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
    "danone",
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
    sale = (chosen.sale_unit or "").upper()
    ptype = (chosen.product_type or "").upper()
    if (
        sale == "KG"
        or ptype == "VARIABLE"
        or chosen.sell_by_weight
        or chosen.price_per_kg
        or _is_weight_unit(unit)
    ):
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
        packs = packs_for_qty(target_qty)

    return packs, round(packs * chosen.price_num, 2)


def packs_for_qty(qty, unit: str | None = None) -> int:
    """Packs for a list quantity; weight items are one line, fractions round up."""
    if (unit or "").lower() in ("kg", "g"):
        return 1
    try:
        return max(1, math.ceil(float(qty) - 1e-9))
    except (TypeError, ValueError):
        return 1


_STOP = frozenset({
    "de", "da", "do", "com", "sem", "para", "em", "e", "ou", "a", "o", "um", "uma",
    "tipo", "pacote", "caixa", "garrafa", "pote", "normal",
})

# Tokens curtos que importam (pó → po; tamanho P).
_SHORT_KEEP = frozenset({"po", "sl", "p", "m", "g"})

_SYNONYMS = {
    "bolacha": ("biscoito", "maizena", "rosquinha"),
    "biscoito": ("bolacha", "maizena"),
    "pao": ("bisnaguinha", "pao"),
    "file": ("peito", "file"),
    "refri": ("refrigerante", "refri", "bioleve"),
    "papel": ("papel", "rolo", "folha"),
    "agua": ("agua", "mineral"),
    "danone": ("danone",),
    "ovo": ("ovo", "ovos"),
    "ovos": ("ovo", "ovos"),
}

_SIZE_ALIASES = {
    "p": ("tamanho p", "tam p", "pequena", "pequeno", "peq"),
    "m": ("tamanho m", "tam m", "media", "medio"),
    "g": ("tamanho g", "tam g", "grande"),
}

# Pedido hortifruti não pode cair em mercearia/bebidas, etc.
_DEPT_REJECT = {
    "hortifruti": (
        "mercearia", "bebidas", "latic", "higiene", "limpeza",
        "padaria", "massas", "carnes e aves",
    ),
    "carnes": ("hortifruti", "mercearia", "bebidas", "higiene", "limpeza", "massas"),
    "higiene": ("hortifruti", "carnes e aves", "latic", "bebidas", "frutas", "massas"),
    "mercearia": ("hortifruti", "carnes e aves", "frutas"),
    "emporio": ("carnes e aves",),
    "laticinios": ("hortifruti", "carnes e aves", "massas"),
}

# Família extra no nome que o pedido não pediu.
_EXTRA_FAMILY = (
    frozenset({"poro"}),
    frozenset({"suco", "nectar", "refresco", "polpa"}),
    frozenset({"macarrao", "espaguete", "nissin"}),
    frozenset({"energetico"}),
    frozenset({"pimentao"}),
    frozenset({"maionese"}),
    frozenset({"passa"}),
    frozenset({"moida", "moido"}),
    frozenset({"codorna"}),
    frozenset({"fit"}),
    frozenset({"condensado"}),
    frozenset({"soja"}),
)


def _has_word(haystack: str, word: str) -> bool:
    h = normalize(haystack)
    w = normalize(word)
    if not w:
        return False
    return re.search(rf"\b{re.escape(w)}\b", h) is not None


def _query_words(enriched_item: dict) -> set[str]:
    bits = [
        enriched_item.get("generic") or "",
        enriched_item.get("item_text") or "",
        enriched_item.get("raw") or "",
        enriched_item.get("flavor") or "",
    ]
    words: set[str] = set()
    for src in bits:
        for tok in normalize(src).split():
            if tok in _STOP or tok.isdigit():
                continue
            if len(tok) >= 3 or tok in _SHORT_KEEP:
                words.add(tok)
                words.update(_SYNONYMS.get(tok, ()))
    return words


def _cats_blob(c: ProductResult) -> str:
    return normalize(" ".join(c.categories or []))


def _is_kg_hit(c: ProductResult) -> bool:
    sale = (c.sale_unit or "").upper()
    ptype = (c.product_type or "").upper()
    return sale == "KG" or ptype == "VARIABLE" or bool(c.sell_by_weight) or bool(c.price_per_kg)


def _department_ok(c: ProductResult, department: str | None, generic: str) -> bool:
    if not department:
        return True
    gen = normalize(generic)
    if gen in ("ovo", "ovos") or "ovo" in gen.split():
        return True
    blob = _cats_blob(c)
    if not blob:
        return True
    for needle in _DEPT_REJECT.get(department, ()):
        if needle in blob:
            return False
    return True


def _unwanted_extra(query_words: set[str], name: str) -> bool:
    n = normalize(name)
    for group in _EXTRA_FAMILY:
        if any(_has_word(n, t) for t in group) and not (query_words & group):
            return True
    # Sal de churrasco "com chimichurri" ≠ tempero chimichurri
    if "chimichurri" in query_words and _has_word(n, "chimichurri"):
        if n.startswith("sal") or "sal para" in n:
            return True
    # Leite em pó: nome precisa do pó (não longa vida / condensado)
    if "po" in query_words and not _has_word(n, "po"):
        return True
    return False


def _name_matches(query_words: set[str], name: str, strict: bool) -> bool:
    if not query_words:
        return True
    strong = {w for w in query_words if len(w) >= 4 or w in _SHORT_KEEP}
    # Tamanho P/M/G e sl não entram no AND (são preferência, não substantivo).
    strong -= {"p", "m", "g", "sl"}
    if strict and strong:
        return all(_word_or_syn(name, w) for w in strong)
    return any(_word_or_syn(name, w) for w in query_words)


def _word_or_syn(name: str, word: str) -> bool:
    if _has_word(name, word):
        return True
    return any(_has_word(name, syn) for syn in _SYNONYMS.get(word, ()))


def _primary_word(generic: str) -> str | None:
    for tok in normalize(generic).split():
        if tok in _STOP:
            continue
        if len(tok) >= 3 or tok in _SHORT_KEEP:
            return tok
    return None


def _primary_ok(primary: str | None, name: str, query_words: set[str]) -> bool:
    if not primary:
        return True
    if _word_or_syn(name, primary):
        return True
    if primary == "papel" and "aluminio" in query_words:
        return _has_word(name, "aluminio") or _has_word(name, "rolo")
    return False


def _wanted_size(enriched_item: dict) -> str | None:
    text = normalize(enriched_item.get("item_text") or enriched_item.get("raw") or "")
    m = re.search(r"\b(pp|gg|xg|p|m|g)\b", text)
    return m.group(1) if m else None


def _size_in_name(name: str, size: str) -> bool:
    n = f" {normalize(name)} "
    if re.search(rf"\b{re.escape(size)}\b", n):
        return True
    return any(alias in n for alias in _SIZE_ALIASES.get(size, ()))


def _passes_gates(
    c: ProductResult,
    query_words: set[str],
    department: str | None,
    generic: str,
    flavor: str,
    strict: bool,
) -> bool:
    primary = _primary_word(generic)
    if not _primary_ok(primary, c.name, query_words):
        return False
    if not _name_matches(query_words, c.name, strict=strict):
        return False
    if not _department_ok(c, department, generic):
        return False
    if _unwanted_extra(query_words, c.name):
        return False
    if flavor:
        flavor_toks = [t for t in re.split(r"\W+", flavor) if len(t) >= 3]
        if flavor_toks and not any(_has_word(c.name, t) for t in flavor_toks):
            return False
    return True


def _filter_relevant(
    candidates: list[ProductResult],
    enriched_item: dict,
) -> list[ProductResult]:
    """Gates: palavra (não substring), departamento Sense, unidade, família extra."""
    generic = enriched_item.get("generic") or enriched_item.get("item_text") or ""
    department = (enriched_item.get("department") or enriched_item.get("category") or "") or None
    unit_expected = (enriched_item.get("unit") or "un").lower()
    query_words = _query_words(enriched_item)
    flavor = (enriched_item.get("flavor") or "").lower()

    filtered = [
        c for c in candidates
        if _passes_gates(c, query_words, department, generic, flavor, strict=True)
    ]
    relaxed = False
    if not filtered:
        relaxed = True
        filtered = [
            c for c in candidates
            if _passes_gates(c, query_words, department, generic, flavor, strict=False)
        ]

    kgish = [c for c in filtered if _is_kg_hit(c)]
    unish = [c for c in filtered if not _is_kg_hit(c)]
    if unit_expected in ("kg", "g") and kgish:
        filtered = kgish
    elif unit_expected == "un" and unish:
        filtered = unish

    if "pipoca" in query_words:
        micro = [
            c for c in filtered
            if _has_word(c.name, "microondas") or _has_word(c.name, "micro")
        ]
        if micro:
            filtered = micro

    if query_words & {"bife", "patinho"}:
        steak = [c for c in filtered if _has_word(c.name, "pedaco")]
        if steak:
            filtered = steak

    size = _wanted_size(enriched_item)
    if size:
        sized = [c for c in filtered if _size_in_name(c.name, size)]
        if sized:
            filtered = sized

    return filtered, relaxed


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
    candidates, relaxed = _filter_relevant(candidates, enriched_item)
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
    if preferred_brand and not rotation and not preferred:
        return Ambiguity(
            kind="missing_brand",
            item_raw=raw,
            reason=f"Marca preferida ({preferred_brand}) não apareceu na busca",
            candidates=candidates[:5],
        )

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

    if (rotation or not preferred_brand) and relaxed and len(candidates) > 1:
        return Ambiguity(
            kind="unsure",
            item_raw=raw,
            reason="Filtro frouxo e mais de um resultado — não chutar",
            candidates=candidates[:5],
        )

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

    return Ambiguity(
        kind="unsure",
        item_raw=raw,
        reason="Sem regra clara para escolher — perguntar",
        candidates=candidates[:5],
    )
