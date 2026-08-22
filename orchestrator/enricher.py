"""
enricher.py — Etapa 2 da Andorinha Shopping Skill (v2)

Lê uma lista de compras raw (MD/TXT) e enriquece cada item cruzando
com o preferencias.yaml (apelidos + marcas).

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
_CHECKBOX_RE = re.compile(r"^[-*•]\s+\[(?: |x|X)\]\s+(.*)$")
_BOLD_HEADER_RE = re.compile(r"^\*\*.*\*\*\s*$")
_EMOJI_RE = re.compile(r"[\U00010000-\U0010FFFF]")
_BOLD_INLINE_RE = re.compile(r"\*+([^*]+)\*+")
_OU_PREFIX_RE = re.compile(r"^ou\s+", re.IGNORECASE)
_QTY_PAREN_RE = re.compile(r"^(.*?)\s*\(([^)]+)\)\s*$")
_QTY_SUFFIX_RE = re.compile(
    r"^(.*?)\s+(\d+(?:[.,]\d+)?\s*(?:kgs?|quilos?|g|gr|gramas?)?)\s*$",
    re.I,
)
_QTY_TOKEN_RE = re.compile(
    r"^(\d+(?:[.,]\d+)?)\s*(kgs?|quilos?|g|gr|gramas?)?$",
    re.I,
)


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


def department_from_heading(line: str) -> str | None:
    n = normalize(_EMOJI_RE.sub("", line).lstrip("#").strip())
    if not n:
        return None
    if "tempero" in n:
        return "emporio"
    if any(x in n for x in ("fruta", "legume", "verdura", "hortifruti")):
        return "hortifruti"
    if any(x in n for x in ("higiene", "limpeza")):
        return "higiene"
    if "carne" in n:
        return "carnes"
    if any(x in n for x in ("mercearia", "despensa")):
        return "mercearia"
    if any(x in n for x in ("laticinio", "laticínio", "iogurte")):
        return "laticinios"
    return None


def parse_list_file(path: Path) -> list[tuple[str, str | None, str | None]]:
    """Lê MD/TXT. Cada item é (texto, flavor|None, department|None)."""
    text = path.read_text(encoding="utf-8")
    raw_items: list[tuple[str, str | None, str | None]] = []
    current_dept: str | None = None

    for line in text.splitlines():
        original = line.strip()
        if not original:
            continue
        if original.startswith("#"):
            current_dept = department_from_heading(original) or current_dept
            continue
        if _BOLD_HEADER_RE.match(original):
            heading_dept = department_from_heading(original)
            if heading_dept:
                current_dept = heading_dept
            continue
        checkbox = _CHECKBOX_RE.match(original)
        if checkbox:
            had_bullet = True
            line = checkbox.group(1).strip()
        else:
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
            raw_items.extend(
                (item_text, flavor, current_dept) for item_text, flavor in _expand_flavors(part)
            )

    seen: set[str] = set()
    unique: list[tuple[str, str | None, str | None]] = []
    for raw, flavor, dept in raw_items:
        key = normalize(extract_qty_from_raw(raw)[0]) + "|" + normalize(flavor or "")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append((raw, flavor, dept))
    return unique


def _parse_qty_token(token: str) -> float | int | None:
    """'2' → 2. '1Kg' / '1 kg' → 1. '200g' → 0.2. 'lixeira grande' → None."""
    m = _QTY_TOKEN_RE.match((token or "").strip())
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "").lower()
    if unit.startswith("k") or unit.startswith("q"):
        if 0.05 <= n <= 50:
            return int(n) if n == int(n) else n
        return None
    if unit.startswith("g"):
        kg = n / 1000.0
        if 0.05 <= kg <= 50:
            return int(kg) if kg == int(kg) else kg
        return None
    if n == int(n) and 1 <= int(n) <= 100:
        return int(n)
    return None


def extract_qty_from_raw(raw: str) -> tuple[str, float | int | None]:
    """'Leite 12' → ('Leite', 12). 'Tomate (1Kg)' → ('Tomate', 1). 'Saco (grande)' não é qty."""
    text = raw.strip()
    m = _QTY_PAREN_RE.match(text)
    if m:
        qty = _parse_qty_token(m.group(2))
        item_text = m.group(1).strip()
        if item_text and qty is not None:
            return item_text, qty
    m = _QTY_SUFFIX_RE.match(text)
    if m:
        qty = _parse_qty_token(m.group(2))
        item_text = m.group(1).strip()
        if item_text and qty is not None:
            return item_text, qty
    return text, None


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
    qty_from_raw: float | int | None
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
    department: str | None = None


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
    Marca entra inteira (depois de tirar 500g/1kg). '3 Corações' permanece.
    """
    generic = (generic or "").strip()
    if not preferred_brand or rotation:
        return generic
    brand = _clean_brand_for_search(preferred_brand)
    if not brand:
        return generic
    gen_norm = normalize(generic)
    if normalize(brand) in gen_norm:
        return generic
    return f"{generic} {brand}".strip()


_KG_WORDS = frozenset({
    "patinho", "acem", "peito", "frango", "coxa", "asa", "bacon",
    "linguica", "pernil", "bisteca", "cenoura", "abobrinha", "batata",
    "banana", "maca", "laranja", "manga", "tomate", "cebola",
    "pepino", "alho", "couve", "carne", "chimichurri", "paprica",
    "calabresa",
})

_FORCE_UN_WORDS = frozenset({
    "molho", "pipoca", "saco", "luva", "leite", "frito", "papel",
    "detergente", "danone", "iogurte", "requeijao", "atum", "sal",
    "acucar", "cafe", "farofa", "bisnaguinha", "ovo", "ovos",
    "alface", "refri", "uva", "melao", "couve",
    "palha", "cogumelo", "espaguete", "espagueti", "filtro",
    "tang", "toddy", "azeitona", "sucrilhos", "bolacha", "biscoito",
    "torrada", "danoninho", "danete", "danette", "amaciante",
    "omo", "ype", "rap", "fita", "folhata", "maizena", "rosquinha",
})

_DAIRY_HINTS = ("leite", "requeijao", "iogurte", "creme de leite", "danone")


def expected_unit(generic: str, department: str | None) -> str:
    words = set(normalize(generic).split())
    if words & _FORCE_UN_WORDS:
        return "un"
    if words & _KG_WORDS:
        return "kg"
    if department == "hortifruti":
        return "kg"
    if department == "carnes" and not (words & {"ovo", "ovos"}):
        return "kg"
    if department in ("mercearia", "higiene", "emporio", "laticinios"):
        return "un"
    return "un"


def enrich_item(
    raw: str,
    prefs: dict[str, Any],
    department: str | None = None,
) -> EnrichedItem:
    item_text, qty_from_raw = extract_qty_from_raw(raw)
    item_for_match = re.sub(r"\(.*?\)", "", item_text).strip() or item_text
    # "Saco de lixo (lixeira pequena)" precisa bater no apelido com o nota,
    # senão cai no genérico 50l.
    norm_full = normalize(item_text)
    norm_key = normalize(item_for_match)

    apelidos = {normalize(k): v for k, v in (prefs.get("apelidos") or {}).items()}
    generic = apelidos.get(norm_full) or apelidos.get(norm_key) or item_for_match
    marcas = {normalize(k): v for k, v in (prefs.get("marcas") or {}).items()}
    brand = (
        marcas.get(normalize(generic))
        or marcas.get(norm_full)
        or marcas.get(norm_key)
    )
    rotation = not bool(brand)
    search_term = _build_search_term(generic, brand, rotation)

    qtys = {normalize(k): v for k, v in (prefs.get("quantidades") or {}).items()}
    pref_qty = qtys.get(norm_full)
    if pref_qty is None:
        pref_qty = qtys.get(norm_key)
    if pref_qty is None:
        pref_qty = qtys.get(normalize(generic))
    if qty_from_raw is not None:
        qty: float | int = qty_from_raw
    elif pref_qty is not None:
        try:
            qty = float(pref_qty)
            if qty == int(qty):
                qty = int(qty)
        except (TypeError, ValueError):
            qty = 1
    else:
        qty = 1

    gen_norm = normalize(generic)
    unit = expected_unit(generic, department)
    dairy = any(h in gen_norm for h in _DAIRY_HINTS) and "ralado" not in gen_norm
    lactose_free = bool(prefs.get("sem_lactose")) and dairy

    notes: list[str] = []
    if department:
        notes.append(f"dept: {department}")
    if brand:
        notes.append(f"marca: {brand}")
    else:
        notes.append("sem marca — mais barato que passar no filtro")
    if lactose_free:
        notes.append("exigir sem lactose")
    if qty_from_raw is None and pref_qty is not None:
        notes.append(f"qty perfil: {qty}")

    return EnrichedItem(
        raw=raw,
        item_text=item_text,
        qty_from_raw=qty_from_raw,
        qty=qty,
        unit=unit,
        generic=generic,
        category=department,
        search_term=search_term,
        preferred_brand=brand,
        lactose_free=lactose_free,
        rotation=rotation,
        kb_matched=bool(brand or norm_key in apelidos),
        match_score=1.0 if (brand or norm_key in apelidos) else 0.0,
        notes=notes,
        department=department,
    )


# =============================================================================
# API PÚBLICA
# =============================================================================


def load_profile(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def enrich_list(list_path: Path, profile_path: Path) -> dict[str, Any]:
    profile = load_profile(profile_path)
    raw_items = parse_list_file(list_path)
    enriched: list[EnrichedItem] = []
    for raw, flavor, department in raw_items:
        if not raw.strip():
            continue
        item = enrich_item(raw, profile, department=department)
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
