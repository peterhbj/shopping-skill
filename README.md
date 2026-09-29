# Andorinha Shopping Skill v3

Lista de compras → carrinho no [andorinhaonline.com.br](https://www.andorinhaonline.com.br). **Checkout é sempre manual.**

O modo CLI legado pode usar `grok -p` para ambiguidades. A skill do Codex usa
`--no-llm` e revisa o plano na própria sessão.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

O Grok Build CLI só é necessário para o fluxo legado com LLM interno.

## Uso

### Pelo Codex CLI, usando a assinatura do ChatGPT

Com o Codex CLI instalado e autenticado com sua conta do ChatGPT, abra este
repositório no terminal e peça: `$andorinha-shopping analise minha lista e
prepare o plano`. A skill do projeto está em `.agents/skills/andorinha-shopping`.
O Codex conduz uma corrida e revisa o plano; o Python faz as buscas e os
cliques, e o Jev pode propor decisões em lote. A skill começa em modo
`--jev-shadow` e não adiciona itens ao carrinho sem um pedido explícito.

O login do Codex usa os limites da sua assinatura, sem chave da API OpenAI.
Jev continua sendo um serviço à parte e requer `TYPESAFE_API_KEY` para ser
usado; sem ela, a skill roda o fluxo local. Se o Codex CLI estiver autenticado
por chave de API em vez do login do ChatGPT, suas chamadas seguem a cobrança
da API. Confira a autenticação antes de rodar. Esta skill roda no computador
onde o Codex CLI e o navegador Playwright estão instalados; o Work no site não
acessa automaticamente o navegador ou o login do Codex do seu computador.

### Jev experimental (planejador em lote)

Defina `TYPESAFE_API_KEY` no ambiente e rode:

```bash
python -m orchestrator.main plan --list lista-compras.md --profile preferencias.yaml --jev-shadow --report-out relatorio.md
```

O enriquecedor local resolve primeiro os itens já cobertos por marca, apelido ou
quantidade do perfil. Se houver termos desconhecidos, o Jev avalia esses itens
em lotes de até 12 contra uma lista curta de produtos conhecidos. Depois da
busca, as ambiguidades restantes também são enviadas em grupos de até 12 itens,
com no máximo cinco produtos elegíveis por pergunta e a opção `ask_user`.

`--jev-shadow` registra as sugestões em `run.json` nos campos `jev_enrichment`
e `jev`, sem alterar o plano. Não resolve `duvidas.md` nem autoriza `apply`.

Com `TYPESAFE_API_KEY` configurada, o CLI usa o Jev em modo automático por
padrão. Use `--jev-shadow` para comparar sem mudar decisões ou `--no-jev` para
desativá-lo naquela execução.

No modo automático, o Jev só mapeia termos e resolve ambiguidades quando a
escolha supera probabilidade e confiança de 0.90, além de margem de 0.20 sobre
a segunda opção. Ajuste com `--jev-min-probability` e `--jev-min-margin`. Casos
incertos continuam para revisão; falhas de API voltam ao fluxo local. Sem a
chave configurada, o fluxo local não chama Jev; `--jev-auto` pode forçar a
tentativa e exige `TYPESAFE_API_KEY`. Não grave a chave no repositório. Ao
escolher manualmente o índice de um candidato, `run.json` guarda
`jev_user_comparison` para comparar a sugestão com sua escolha.

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
