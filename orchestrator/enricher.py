"""
enricher.py — Etapa 2 da Andorinha Shopping Skill (v2)

Lê uma lista de compras raw (MD/TXT) e enriquece cada item cruzando
com o perfil-compras.yaml (vocabulary, direct_search, products).

CLI:
    python3 -m orchestrator.enricher \\
        --list lista-compras.md \\
        --profile perfil-compras.yaml \\
        --output /tmp/enriched.json

Programático:
    from orchestrator.enricher import enrich_list
    enriched = enrich_list(list_path, profile_path)

Saída: dict com {meta, items} — items contém um dict por item com
search_term, qty, preferred_brand, lactose_free, rotation, kb_matched, etc.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import yaml


# =============================================================================
# PARSING DA LISTA RAW
# =============================================================================

_BULLET_RE = re.compile(r"^[-*•]\s+")
_BOLD_HEADER_RE = re.compile(r"^\*\*.*\*\*\s*$")
_EMOJI_RE = re.compile(r"[\U00010000-\U0010FFFF]")
_BOLD_INLINE_RE = re.compile(r"\*+([^*]+)\*+")
_OU_PREFIX_RE = re.compile(r"^ou\s+", re.IGNORECASE)
_QTY_SUFFIX_RE = re.compile(r"^(.*?)\s+(\d+)\s*$")


def _expand_flavors(part: str) -> list[tuple[str, str | None]]:
    """
    'Pipoca microondas: manteiga, só sal, tempero do chef'
    → três pares (base, flavor). Sem ':' ou sem lista → um item.
    """
    if ":" not in part:
        return [(part, None)]
    left, right = part.split(":", 1)
    left = left.strip()
    flavors = [f.strip() for f in right.split(",") if f.strip()]
    if not left or not flavors:
        return [(part, None)]
    # Um único rótulo longo depois de ':' (ex. horário) não é lista de sabores.
    if len(flavors) == 1 and len(flavors[0].split()) > 4:
        return [(part, None)]
    return [(left, f) for f in flavors]


def parse_list_file(path: Path) -> list[tuple[str, str | None]]:
    """Lê MD/TXT. Cada item é (texto, flavor|None)."""
    text = path.read_text(encoding="utf-8")
    raw_items: list[tuple[str, str | None]] = []

    for line in text.splitlines():
        original = line.strip()
        if not original or original.startswith("#"):
            continue
        if _BOLD_HEADER_RE.match(original):
            continue
        had_bullet = bool(_BULLET_RE.match(original))
        line = _BULLET_RE.sub("", original).strip()
        if not line:
            continue
        if not had_bullet:
            words = line.split()
            if (
                len(words) <= 3
                and not re.search(r"\d", line)
                and "(" not in line
                and re.match(r"^[A-ZÁÉÍÓÚÀÂÊÔÇÃÕÜ]", line)
            ):
                continue
        line = "".join(c for c in line if ord(c) < 0x2500 or ord(c) > 0x26FF)
        line = _EMOJI_RE.sub("", line).strip()
        line = _BOLD_INLINE_RE.sub(r"\1", line).strip()

        parts = [p.strip() for p in line.split(" / ") if p.strip()]
        for part in parts:
            if _OU_PREFIX_RE.match(part):
                continue
            raw_items.extend(_expand_flavors(part))

    seen: set[str] = set()
    unique: list[tuple[str, str | None]] = []
    for raw, flavor in raw_items:
        key = normalize(extract_qty_from_raw(raw)[0]) + "|" + normalize(flavor or "")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append((raw, flavor))
    return unique


def extract_qty_from_raw(raw: str) -> tuple[str, int | None]:
    """'Leite 12' → ('Leite', 12). 'Queijo ralado' → ('Queijo ralado', None)."""
    m = _QTY_SUFFIX_RE.match(raw.strip())
    if m:
        item_text = m.group(1).strip()
        qty = int(m.group(2))
        if item_text and 1 <= qty <= 100:
            return item_text, qty
    return raw.strip(), None


# =============================================================================
# NORMALIZAÇÃO E FUZZY MATCHING
# =============================================================================

_ACCENT_MAP = str.maketrans(
    {
        "á": "a", "à": "a", "â": "a", "ã": "a",
        "é": "e", "ê": "e", "è": "e",
        "í": "i", "î": "i",
        "ó": "o", "ô": "o", "õ": "o",
        "ú": "u", "û": "u",
        "ç": "c", "ñ": "n",
    }
)


def normalize(text: str) -> str:
    text = text.lower().strip().translate(_ACCENT_MAP)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def find_best_kb_match(
    item_text: str,
    products: list[dict],
    vocabulary: dict[str, str],
) -> tuple[dict | None, float]:
    """Retorna (produto, score 0-1). Aplica vocabulary antes do fuzzy."""
    norm_item = normalize(item_text)
    alias = vocabulary.get(norm_item) or vocabulary.get(item_text.lower().strip())
    if alias:
        norm_item = normalize(alias)

    best_product: dict | None = None
    best_score = 0.0

    for product in products:
        generic = product.get("generic", "")
        norm_generic = normalize(generic)
        score = difflib.SequenceMatcher(None, norm_item, norm_generic).ratio()

        item_words = set(norm_item.split())
        generic_words = set(norm_generic.split())
        if generic_words:
            word_overlap = len(item_words & generic_words) / len(generic_words)
            score = max(score, word_overlap * 0.8)

        if norm_item in norm_generic or norm_generic in norm_item:
            score = max(score, 0.75)
        if len(norm_item) <= 5 and norm_item in norm_generic:
            score = max(score, 0.7)

        if score > best_score:
            best_score = score
            best_product = product

    return best_product, best_score


# =============================================================================
# ENRIQUECIMENTO POR ITEM
# =============================================================================


@dataclass
class EnrichedItem:
    raw: str
    item_text: str
    qty_from_raw: int | None
    qty: float | int
    unit: str
    generic: str | None
    category: str | None
    search_term: str
    preferred_brand: str | None
    lactose_free: bool
    rotation: bool
    kb_matched: bool
    match_score: float
    notes: list[str]
    flavor: str | None = None


def _clean_brand_for_search(brand: str) -> str:
    """Remove tamanho/volume da preferred_brand para usar como search."""
    s = re.sub(r"\s+\d+[gGlLkKmM][gGlLmM]?\b", "", brand).strip()
    s = re.sub(r"\s+\d+(?:[.,]\d+)?\s*(?:un|unid|rolos?|fls?)\b", "", s, flags=re.I).strip()
    return s or brand


def _brand_core(brand: str) -> str:
    """Extrai só tokens de marca fortes (sem tamanho/oferta)."""
    weak = {
        "leve", "pague", "pacote", "caixa", "lata", "rolo", "original", "tradicional",
        "integral", "tipo", "vácuo", "vacuo", "refil", "garrafa", "pote", "cx", "pct",
        "folha", "dupla", "tripla", "neutro", "oferta", "semidesnatado", "desnatado",
        "extra", "fino", "fina", "grande", "pequeno",
        "emb", "economica", "economico", "econômica", "econômico",
    }
    tokens = []
    for t in re.split(r"\W+", brand.lower()):
        if len(t) < 3 or t in weak or re.search(r"\d", t):
            continue
        if re.fullmatch(r"(?:ml|kg|g|lt|un|unid|gr)", t):
            continue
        tokens.append(t)
    return " ".join(tokens[:3])


def _build_search_term(generic: str, preferred_brand: str | None, rotation: bool) -> str:
    """
    Busca = produto + marca (quando houver), nunca só a marca.
    Evita: search='Heinz' → maionese; search='Yoki' → pipoca; search='Camil' → feijão.
    """
    generic = (generic or "").strip()
    if not preferred_brand or rotation:
        return generic
    core = _brand_core(preferred_brand)
    if not core:
        return generic
    # Se a marca já aparece no generic, não duplica
    gen_norm = normalize(generic)
    if normalize(core) in gen_norm:
        return generic
    gen_tokens = set(gen_norm.split())
    extra = [t for t in core.split() if normalize(t) not in gen_tokens]
    if not extra:
        return generic
    return f"{generic} {' '.join(extra)}".strip()


def enrich_item(
    raw: str,
    products: list[dict],
    vocabulary: dict[str, str],
    direct_search: dict[str, dict],
    match_threshold: float,
) -> EnrichedItem:
    item_text, qty_from_raw = extract_qty_from_raw(raw)
    item_for_match = re.sub(r"\(.*?\)", "", item_text).strip()
    norm_key = normalize(item_for_match)

    # 1) direct_search override
    direct = direct_search.get(norm_key) or direct_search.get(
        item_for_match.lower().strip()
    )
    if direct:
        notes = list(direct.get("notes", [])) + ["busca direta"]
        unit = direct.get("unit") or (
            "kg" if any(k in norm_key for k in ("suino", "copa", "bisteca")) else "un"
        )
        qty = qty_from_raw or direct.get("qty") or 1
        return EnrichedItem(
            raw=raw,
            item_text=item_text,
            qty_from_raw=qty_from_raw,
            qty=qty,
            unit=unit,
            generic=direct.get("generic"),
            category=direct.get("category"),
            search_term=direct["search"],
            preferred_brand=direct.get("preferred_brand"),
            lactose_free=bool(direct.get("lactose_free", False)),
            rotation=bool(direct.get("rotation", False)),
            kb_matched=bool(direct.get("kb_matched", False)),
            match_score=1.0 if direct.get("kb_matched") else 0.0,
            notes=notes,
        )

    # 2) Fuzzy KB match
    kb_product, score = find_best_kb_match(item_for_match, products, vocabulary)
    kb_matched = kb_product is not None and score >= match_threshold

    if not kb_matched:
        notes = [f"item não encontrado no KB (score={score:.2f})"]
        if "(" in raw and ")" in raw:
            m = re.search(r"\(.*?\)", raw)
            if m:
                notes.append(f"nota original: {m.group()}")
        return EnrichedItem(
            raw=raw,
            item_text=item_text,
            qty_from_raw=qty_from_raw,
            qty=qty_from_raw or 1,
            unit="un",
            generic=None,
            category=None,
            search_term=item_for_match,
            preferred_brand=None,
            lactose_free=False,
            rotation=True,
            kb_matched=False,
            match_score=round(score, 2),
            notes=notes,
        )

    # 3) KB match válido
    assert kb_product is not None
    preferred_brand = kb_product.get("preferred_brand") or ""
    typical_qty = kb_product.get("typical_qty") or 1
    unit = kb_product.get("unit", "un")
    generic = kb_product.get("generic", "")
    category = kb_product.get("category", "")
    lactose_free = str(kb_product.get("lactose_free", "false")).lower() == "true"
    rotation = str(kb_product.get("rotation", "false")).lower() == "true"

    if qty_from_raw is not None:
        final_qty: float | int = qty_from_raw
    else:
        final_qty = (
            int(typical_qty) if isinstance(typical_qty, (int, float)) and typical_qty == int(typical_qty) else typical_qty
        )

    search_term = _build_search_term(generic, preferred_brand or None, rotation)

    notes: list[str] = []
    if "(" in raw and ")" in raw:
        m = re.search(r"\(.*?\)", raw)
        if m:
            notes.append(f"nota original: {m.group()}")
    if score < 0.65:
        notes.append(f"match baixa confiança (score={score:.2f})")
    if rotation:
        notes.append("marca flexível — qualquer marca similar serve")

    return EnrichedItem(
        raw=raw,
        item_text=item_text,
        qty_from_raw=qty_from_raw,
        qty=final_qty,
        unit=unit,
        generic=generic,
        category=category,
        search_term=search_term,
        preferred_brand=preferred_brand or None,
        lactose_free=lactose_free,
        rotation=rotation,
        kb_matched=True,
        match_score=round(score, 2),
        notes=notes,
    )


# =============================================================================
# API PÚBLICA
# =============================================================================


def load_profile(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def enrich_list(list_path: Path, profile_path: Path) -> dict[str, Any]:
    profile = load_profile(profile_path)
    products = profile.get("products", [])
    vocabulary = profile.get("vocabulary", {}) or {}
    direct_search = profile.get("direct_search", {}) or {}
    config = profile.get("config", {}) or {}
    threshold = float(
        (config.get("enrichment") or {}).get("match_threshold", 0.45)
    )

    raw_items = parse_list_file(list_path)
    enriched: list[EnrichedItem] = []
    for raw, flavor in raw_items:
        if not raw.strip():
            continue
        item = enrich_item(raw, products, vocabulary, direct_search, threshold)
        item.flavor = flavor
        if flavor:
            item.raw = f"{item.raw} ({flavor})"
            extra = flavor.strip()
            if extra.lower() not in item.search_term.lower():
                item.search_term = f"{item.search_term} {extra}".strip()
            item.notes = list(item.notes) + [f"sabor: {flavor}"]
        enriched.append(item)
    matched = sum(1 for i in enriched if i.kb_matched)

    return {
        "meta": {
            "input_file": str(list_path),
            "profile_file": str(profile_path),
            "total_items": len(enriched),
            "kb_matched": matched,
            "unmatched": len(enriched) - matched,
        },
        "items": [asdict(i) for i in enriched],
    }


# =============================================================================
# CLI
# =============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description="Enriquece lista de compras com perfil-compras.yaml")
    parser.add_argument("--list", required=True, help="Lista de compras (MD/TXT)")
    parser.add_argument("--profile", required=True, help="perfil-compras.yaml")
    parser.add_argument("--output", default=None, help="JSON de saída (default: stdout)")
    args = parser.parse_args()

    result = enrich_list(Path(args.list), Path(args.profile))
    output_json = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        Path(args.output).write_text(output_json, encoding="utf-8")
        meta = result["meta"]
        print(
            f"✅ {meta['total_items']} itens enriquecidos "
            f"({meta['kb_matched']} matched, {meta['unmatched']} unmatched) → {args.output}",
            file=sys.stderr,
        )
    else:
        print(output_json)

    return 0


if __name__ == "__main__":
    sys.exit(main())
