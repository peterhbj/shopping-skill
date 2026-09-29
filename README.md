# Andorinha Shopping Skill v3

Lista de compras → carrinho no [andorinhaonline.com.br](https://www.andorinhaonline.com.br). **Checkout é sempre manual.**

O modo CLI legado pode usar `grok -p` para ambiguidades. A skill do Codex usa
`--no-llm` e revisa o plano na própria sessão.

## Aplicativo local no Windows

Na raiz do projeto, execute `powershell -ExecutionPolicy Bypass -File .\start_windows.ps1`.
Na primeira execução, o script cria `.venv` e instala `requirements.txt`.
No Windows usa o Microsoft Edge instalado com um perfil separado; o Chromium
baixado pelo Playwright pode ser bloqueado pelo Controle de Aplicativo. A
interface abre em `http://127.0.0.1:8765` e não fica
disponível na rede.

As dúvidas são explicadas por um modelo pequeno. O padrão é o Claude Haiku via
`claude -p`, sem ferramentas, usando o login do Claude CLI (assinatura Pro/Max):
rode `claude` uma vez no terminal para entrar. Uma `ANTHROPIC_API_KEY` no
ambiente é ignorada nessa chamada para não cobrar da API. Para outro modelo,
defina `ANDORINHA_CLAUDE_MODEL` (ex.: `sonnet`). Para usar o Luna pelo Codex,
defina `ANDORINHA_REVIEWER=luna` e tenha o `codex` no PATH.

O aplicativo usa um diretório Codex pessoal isolado em
`%LOCALAPPDATA%\AndorinhaShopping\codex-home-personal`, separado do login do
Codex desktop. Conecte sua conta ChatGPT por código de dispositivo na
interface. O histórico e os planos ficam em `%LOCALAPPDATA%\AndorinhaShopping`;
o perfil do navegador fica no diretório temporário do Windows. Nenhum desses
arquivos vai para o Git. A chave do Jev inserida na interface dura apenas até
fechar o aplicativo.

Entre no Andorinha pela janela de login do aplicativo e use **Sincronizar
pedidos**. A loja não publica uma exportação documentada: a sincronização lê
somente objetos de pedidos concluídos encontrados nas páginas autenticadas.
Se nenhum detalhe estiver disponível, importe um JSON com `orders` ou um CSV
com colunas `order_id,ordered_at,status,product_id,name,quantity,unit,unit_price`.
Cada pedido deve ter status `entregue` ou `concluído` (também aceita
`delivered` ou `completed`). Importar o mesmo `order_id` novamente não duplica
linhas. O aplicativo mostra quando o histórico está incompleto.

**Gerar plano** consulta o catálogo sem clicar no carrinho. As sugestões do
Jev ficam em modo de observação. O assistente explica as dúvidas; se ele estiver
indisponível, as opções continuam na interface. Antes de **Adicionar ao
carrinho**, resolva ou pule cada pendência. O navegador procura novamente o
produto exato e bloqueia o clique se o preço mudou. Checkout é manual.

Para medir escolhas automáticas, prepare `casos.json` com casos datados. Cada
caso precisa de `request`, `ordered_at`, `expected_product_id` e `candidates`
com os produtos que estavam disponíveis naquela data (`product_id`, `name`,
`price_num`, `sale_unit` e, se houver, `brand_name`). Execute
`python -m orchestrator.evaluate casos.json`. O replay usa apenas compras
anteriores a `ordered_at` e informa acertos, cobertura e o limite inferior de
confiança de 95% da precisão. A meta de 98% só é marcada como sustentada
quando esse limite atinge 98%; sem listas e catálogos antigos, ainda não há
amostra para essa medição. Compare o tempo da mesma lista no CLI antigo e no
aplicativo, com a mesma sessão e conexão, antes de afirmar redução de 50%.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

O Grok Build CLI só é necessário para o fluxo legado com LLM interno.

### WSL

Execute os comandos dentro do WSL, na raiz deste repositório. O Chromium do
Playwright abre uma janela pelo WSLg; confira com
`python -m orchestrator.browser "papel higienico"` antes de planejar a lista.
Se o Chromium falhar com `Operation not permitted` ao executar pelo Codex,
autorize a execução fora do sandbox quando solicitada. Se falhar também no
terminal WSL comum, confira `echo "$DISPLAY"` e atualize o WSLg com
`wsl --update` no PowerShell, seguido de `wsl --shutdown` e nova abertura do
WSL. O perfil de login do Playwright é separado do Chrome do Windows: use
`python -m orchestrator.browser --login` uma vez para entrar no Andorinha.

Para usar o Jev, a chave precisa estar no ambiente do **mesmo terminal WSL**
que executa o projeto. Se for pedir ao Codex para rodar o plano, configure a
chave antes de iniciar o Codex nesse terminal. Informe-a sem gravá-la no
repositório:

```bash
read -rsp 'TYPESAFE_API_KEY: ' TYPESAFE_API_KEY; echo
export TYPESAFE_API_KEY
test -n "$TYPESAFE_API_KEY" && echo 'Jev configurado'
curl -sS -o /dev/null -w 'TypeSafe HTTP %{http_code}\n' -H "Authorization: Bearer $TYPESAFE_API_KEY" https://api.typesafe.ai/v1/models
python -m orchestrator.main plan --list lista-compras.md --profile preferencias.yaml --no-llm --jev-shadow --report-out relatorio.md
```

O teste da API deve mostrar `TypeSafe HTTP 200`. Se mostrar `401`, confira se a
chave está ativa e se foi colada sem o prefixo `Bearer`; o `plan` não conseguirá
usar o Jev enquanto a autenticação falhar.

O comando `plan` só pesquisa e gera `run.json`, `relatorio.md` e possíveis
dúvidas; não adiciona produtos ao carrinho. No `--jev-shadow`, as sugestões do
Jev são registradas para revisão, sem mudar as escolhas.

## Uso

### Pelo Codex CLI, usando a assinatura do ChatGPT

Com o Codex CLI instalado e autenticado com sua conta do ChatGPT, abra este
repositório no terminal WSL com GPT-6 Luna:

```bash
# Execute na raiz deste repositório.
source .venv/bin/activate
read -rsp 'TYPESAFE_API_KEY (sem o prefixo Bearer): ' TYPESAFE_API_KEY; echo
export TYPESAFE_API_KEY
curl -sS -o /dev/null -w 'TypeSafe HTTP %{http_code}\n' -H "Authorization: Bearer $TYPESAFE_API_KEY" https://api.typesafe.ai/v1/models
codex -m gpt-6-luna
```

No Codex, confirme o modelo com `/status` e peça: `$andorinha-shopping analise
minha lista e prepare o plano`. A skill do projeto está em
`.agents/skills/andorinha-shopping`.
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
