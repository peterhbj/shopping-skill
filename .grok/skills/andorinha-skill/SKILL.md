---
name: andorinha-skill
description: >
  Monta o carrinho no Andorinha a partir de lista-compras.md e perfil-compras.yaml. O script chama grok -p nas ambiguidades. O agente é conselheiro: plan/apply, alinhamento ao perfil, checagem final. Nunca finaliza a compra. Use when the user runs /andorinha-skill, or mentions compras Andorinha, montar carrinho, lista de compras, or revisar carrinho.
---

# Andorinha shopping advisor

Repo root = pasta com `lista-compras.md`, `perfil-compras.yaml` e `orchestrator/`.

## Hard rules

- Never checkout. Never click finalizar or pay.
- Never `--no-keep-open` on `apply` / `run`.
- Preferences live in `perfil-compras.yaml`. Do not invent brands.
- Ask the user when unsure. Persist lasting answers in the YAML (`vocabulary` / `direct_search`). One-off qty stays in the list.
- Ambiguities are resolved by `grok -p` inside the script (not Claude). If the CLI fails, read `run.json` and decide with the user.

## List syntax

- `Torrada / maionese` — two different products.
- `Pipoca microondas: manteiga, só sal, tempero do chef` — same product, three flavors (three cart lines).
- `Leite 12` — qty 12.

## Commands

```bash
python -m orchestrator.main plan --list lista-compras.md --profile perfil-compras.yaml --report-out relatorio.md
python -m orchestrator.main apply --list lista-compras.md --profile perfil-compras.yaml --report-out relatorio.md
python -m orchestrator.main run --list lista-compras.md --profile perfil-compras.yaml --report-out relatorio.md
```

`plan` only searches (writes `run.json`). `apply` only clicks decided rows. `run` = plan + grok -p + apply.

Login (Playwright profile, not normal Chrome):

```bash
python -m orchestrator.browser --login
```

## Workflow

1. Enrich / `plan`. Summarize unmatched, `needs_grok`, wrong-family risks.
2. Confirm login if needed.
3. `run` (or `apply` after you edited `run.json`). Long timeout. Watch `[browser] set_qty SEM ACK` and session warnings.
4. Last check from `relatorio.md` + logs: added vs asked, kg items (1 click — user sets weight), `failed_to_add` / `not_found`, lactose, flavors.
5. Leave the window open. User reviews and checks out.

## Do not

- Live-add without a plan/`run.json` unless the user skips it.
- Re-`apply` the same list blindly — apply only clicks the missing qty, but leftovers from a broken run should be emptied first.
- Document setup here. Missing deps: `pip install -r requirements.txt`, `playwright install chromium`, `grok` on PATH.
