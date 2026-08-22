---
name: andorinha-skill
description: >
  Monta o carrinho no Andorinha a partir de lista-compras.md e preferencias.yaml. O script chama grok -p nas ambiguidades. O agente é conselheiro: plan/apply, alinhamento ao perfil, checagem final. Nunca finaliza a compra. Use when the user runs /andorinha-skill, or mentions compras Andorinha, montar carrinho, lista de compras, or revisar carrinho.
---

# Andorinha shopping advisor

Repo root = pasta com `lista-compras.md`, `preferencias.yaml` e `orchestrator/`.
Fonte viva: `preferencias.yaml` (`apelidos` + `marcas` + `quantidades` + `sem_lactose`).
Número na lista manda nesta compra (`Sal (2)`, `Tomate (1kg)`, `Leite 12`). Sem número, o enricher usa `quantidades`. Não defaultar 1 nem marca mais barata se o YAML já tem regra.
`perfil-compras.yaml` é o histórico de 10 pedidos que originou essas regras — não usar no runtime no lugar do YAML curto.
Itens em kg (carne, hortifruti granel): o site só aceita **1 clique**. `quantidades` é o peso alvo na balança, não o número de cliques. O carrinho entra com o peso padrão do site (muitas vezes 200–400g) — o usuário ajusta pro kg do relatório.

## Hard rules

- Never checkout. Never click finalizar or pay.
- Never `--no-keep-open` on `apply` / `run`.
- Preferences live in `preferencias.yaml`. Do not invent brands.
- Ask the user when unsure. Lasting brand/qty → `marcas` / `quantidades` / `apelidos`. One-off qty stays in the list.
- Dúvida não vai pro carrinho e **não** chama LLM. O `plan` segue o próximo item, grava em `duvidas.md` e pergunta. Só depois das respostas: `resolve` → grok -p → busca de novo.
- Água = Bioleve **com gás** (nunca sem gás). Creme/requeijão/leite = SL. Pipoca = microondas, um pacote por sabor da lista. Refri Bioleve = 6 por sabor. Iogurte na lista = Danone morango 1,25 kg (não segundo iogurte).

## List syntax

- `Torrada / maionese` — two different products.
- `Pipoca microondas: manteiga, só sal, tempero do chef` — same product, three flavors (three cart lines).
- `Leite 12` — qty 12.
- `Tomate (1kg)` / `Chimichurri (200g)` — peso. Sem unidade, `(2)` é unidades. `(lixeira grande)` não é qty.

## Commands

```bash
python -m orchestrator.main plan --list lista-compras.md --profile preferencias.yaml --report-out relatorio.md
python -m orchestrator.main resolve --list lista-compras.md --profile preferencias.yaml --answers respostas.yaml --report-out relatorio.md
python -m orchestrator.main apply --list lista-compras.md --profile preferencias.yaml --report-out relatorio.md
python -m orchestrator.main run --list lista-compras.md --profile preferencias.yaml --report-out relatorio.md
```

`plan` busca e só decide o que está claro. No terminal (TTY), as dúvidas vêm **no próprio stdin** antes de qualquer ENTER do carrinho. Sem TTY (agente), grava `duvidas.md`. `resolve` / respostas no prompt → grok -p → busca de novo. `apply` só clica `decided`. `run` no terminal = plan → perguntas → resolve → apply → ENTER pra revisar o carrinho.

Login (Playwright profile, not normal Chrome):

```bash
python -m orchestrator.browser --login
```

## Workflow

1. Enrich / `plan`. Show `duvidas.md`: busca, o que apareceu, por quê não decidiu.
2. Ask the user those items in chat. Persist lasting answers in `preferencias.yaml`. Write `respostas.yaml`.
3. `resolve --answers respostas.yaml`. Then `apply` only if nothing is `needs_user`. Long timeout.
4. Last check from `relatorio.md` + logs: added vs asked, kg items (1 click — user sets weight), `failed_to_add` / `not_found`, lactose, flavors.
5. Leave the window open. User reviews and checks out.

## Do not

- Live-add without a plan/`run.json` unless the user skips it.
- Re-`apply` the same list blindly — apply only clicks the missing qty, but leftovers from a broken run should be emptied first.
- Document setup here. Missing deps: `pip install -r requirements.txt`, `playwright install chromium`, `grok` on PATH.
