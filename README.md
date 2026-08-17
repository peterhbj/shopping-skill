# Andorinha Shopping Skill v3

Lista de compras → carrinho no [andorinhaonline.com.br](https://www.andorinhaonline.com.br). **Checkout é sempre manual.**

Ambiguidades (marca preferida vs oferta ≥15% mais barata) vão para `grok -p`. Sem Claude.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

O Grok Build CLI precisa estar no PATH (`grok --version`) e logado (`grok login`).

## Uso

```bash
# Tudo: busca + grok nas dúvidas + adiciona (janela fica aberta)
python -m orchestrator.main run --list lista-compras.md --profile preferencias.yaml --report-out relatorio.md

# Só buscar (grava run.json, não clica)
python -m orchestrator.main plan --list lista-compras.md --profile preferencias.yaml --report-out relatorio.md

# Só clicar o que já está decidido em run.json
python -m orchestrator.main apply --list lista-compras.md --profile preferencias.yaml --report-out relatorio.md

# Sem grok -p (auto marca preferida)
python -m orchestrator.main run --list lista-compras.md --profile preferencias.yaml --no-llm

# Login no Chromium do Playwright (não é o Chrome normal)
python -m orchestrator.browser --login
```

Perfil persistente: `%TEMP%\andorinha-pw-profile` (Windows) ou `/tmp/andorinha-pw-profile`. Sempre `--profile preferencias.yaml` (não o `perfil-compras.yaml` antigo).

Se o carrinho da corrida anterior ainda tiver item, esvazie antes de um teste. `apply` só dá os cliques que faltam para a qty alvo.

## Lista

```markdown
- Torrada / maionese          → dois produtos
- Pipoca microondas: manteiga, só sal, tempero do chef
- Leite 12                    → 12 unidades
```

` / ` = outro produto. `Produto: a, b, c` = mesmo generic, três sabores (três linhas no carrinho).

## Pipeline

1. **Enricher** — lista + seção (`## Hortifruti`) + `preferencias.yaml`
2. **plan** — busca Sense (categoria + `saleUnit`) + regras (família, palavra, lactose, marca)
3. **grok -p** — só se a preferida tiver alternativa ≥15% mais barata/unidade
4. **apply** — clica `+` e espera ack: card com botão `−` + qty estável (não o número otimista)
5. **Relatório** — markdown. Drawer vazio = carrinho não lido, não “deu certo”.

Itens em kg: em geral 1 clique; o peso você ajusta no site.

## Estrutura

```
shopping-skill/
├── orchestrator/          # main, browser, enricher, selector, report, llm/
├── scripts/browser_helpers.js
├── preferencias.yaml
├── lista-compras.md
└── .grok/skills/andorinha-skill/
```
