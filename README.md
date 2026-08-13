# Andorinha Shopping Skill v2.1

Automação de lista de compras → carrinho no [andorinhaonline.com.br](https://www.andorinhaonline.com.br).

**LLM é opcional.** Por padrão zero chamada de modelo (só regras determinísticas + auto-resolve de ambiguidades).

## Setup rápido

```bash
cd shopping-skill
pip install -r requirements.txt
playwright install chromium
```

## Uso

```bash
# Dry-run (não mexe no carrinho) — recomendado na primeira vez
python -m orchestrator.main \
  --list lista-compras.md \
  --profile perfil-compras.yaml \
  --report-out relatorio.md \
  --dry-run

# Rodar de verdade (abre Chromium, adiciona ao carrinho)
python -m orchestrator.main \
  --list lista-compras.md \
  --profile perfil-compras.yaml \
  --report-out relatorio.md

# Usar Chrome já aberto com remote debugging (CDP)
python -m orchestrator.main \
  --list lista-compras.md \
  --profile perfil-compras.yaml \
  --cdp

# Habilitar LLM só quando quiser gastar token em ambiguidades difíceis
python -m orchestrator.main \
  --list lista-compras.md \
  --profile perfil-compras.yaml \
  --use-llm
```

## Pipeline

1. **Enricher** — lê a lista + `perfil-compras.yaml` (vocabulary, direct_search, products)
2. **Selector** — regras determinísticas:
   - filtro lactose (household)
   - pack optimization (custo/benefício)
   - preferred brand
   - dominance de preço (≥15% mais barato → ambiguidade)
   - rotation / cheapest
3. **Ambiguity**:
   - sem `--use-llm` → auto-resolve (preferred ou cheapest)
   - com `--use-llm` → Claude CLI
4. **Browser** (Playwright) — busca + `set_qty`
5. **Report** — markdown com status de cada item

## Estrutura

```
shopping-skill/
├── orchestrator/
│   ├── main.py
│   ├── browser.py
│   ├── enricher.py
│   ├── selector.py
│   ├── report.py
│   └── llm/
│       ├── adapter.py
│       └── prompts/resolve_ambiguity.md
├── scripts/browser_helpers.js
├── perfil-compras.yaml
├── lista-compras.md
└── requirements.txt
```

## Notas importantes

- **Checkout é manual.** A skill só monta o carrinho.
- Itens vendidos por kg (carnes/hortifruti) são adicionados com qty=1 no card; o peso final ainda precisa de ajuste manual no site em alguns casos.
- Site muda → seletores quebram. O helper usa `.item-product-wrapper` e aria-labels; se o Andorinha mudar o layout, vai precisar de ajuste.
- Não finaliza pedido e não digita cartão. Só adiciona.

## Grok Build / terminal

Dentro do Grok Build ou qualquer terminal:

```bash
cd /caminho/para/shopping-skill
python -m orchestrator.main --list lista-compras.md --profile perfil-compras.yaml --dry-run
```

Se der erro de display no headless, o Playwright lança Chromium com perfil persistente em `%TEMP%\andorinha-pw-profile` (Windows) ou `/tmp/andorinha-pw-profile` (Unix). O relatório default vai para o mesmo diretório temporário (`andorinha_report.md`).

Para adicionar ao carrinho de verdade, o Chromium do Playwright precisa de sessão logada (é um perfil separado do Chrome normal):

```bash
python -m orchestrator.browser --login
```

Faça login na janela que abrir e pressione ENTER no terminal. O cookie fica em `%TEMP%\andorinha-pw-profile` (Windows) ou `/tmp/andorinha-pw-profile` (Unix). Depois rode o orchestrator sem `--dry-run`.

Alternativa: Chrome já logado com `--remote-debugging-port=9222` e a flag `--cdp`.
