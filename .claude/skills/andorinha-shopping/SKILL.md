---
name: andorinha-shopping
description: Planejar e revisar compras do Andorinha com a lista, preferencias.yaml, Jev e o orquestrador Python deste repositório. Use ao pedir para analisar a lista, comparar produtos, montar ou revisar o carrinho, ou executar uma compra assistida.
---

# Compras assistidas

Trabalhe na raiz deste repositório (pasta com `lista-compras.md`, `preferencias.yaml` e `orchestrator/`). Você é o coordenador de uma corrida, não um subprocesso para cada produto. Leia `lista-compras.md`, `preferencias.yaml` e o `README.md` antes de planejar. Não exiba credenciais, dados de autenticação ou arquivos de perfil do navegador.

## Regras fixas

- Nunca finalize a compra. Checkout e pagamento são sempre manuais.
- Nunca use `--no-keep-open` em `apply` ou `run`.
- As preferências vivem em `preferencias.yaml` (`apelidos`, `marcas`, `quantidades`, `sem_lactose`). Não invente marcas. `perfil-compras.yaml` é só o histórico que originou essas regras.
- Número na lista manda nesta compra (`Sal (2)`, `Tomate (1kg)`, `Leite 12`). Sem número, vale `quantidades`.
- Itens em kg aceitam um clique só; o peso o usuário ajusta no site.
- Na dúvida, pergunte. Resposta duradoura vai para `preferencias.yaml`; quantidade pontual fica na lista.

## Passos

1. Confira a lista e as preferências. Aponte apenas inconsistências que afetem a escolha; preserve quantidades, sabores, restrições e marcas explicitamente pedidas.
2. Execute `python -m orchestrator.main plan --list lista-compras.md --profile preferencias.yaml --no-llm --jev-shadow --report-out relatorio.md` se `TYPESAFE_API_KEY` estiver configurada. Sem a chave, use `--no-jev` no lugar de `--jev-shadow`. Não leia nem imprima o valor da chave.
3. Leia `run.json`, `relatorio.md` e `duvidas.md` se existir. Revise também as escolhas automáticas feitas pelo modo `--no-llm`: marca ou menor preço podem não corresponder ao pedido. Compare as sugestões do Jev com os candidatos e o perfil. Jev em modo shadow apenas registra sugestões; confiança alta não garante que o produto corresponda à lista. Não escolha candidatos fora dos resultados pesquisados.
4. Para dúvidas, explique opções e diferenças de preço/unidade ao usuário. Com informação suficiente ou uma escolha expressa, prepare `respostas.yaml` no formato `respostas: [{item: "texto do item", texto: "índice do candidato"}]` e execute `python -m orchestrator.main resolve --list lista-compras.md --profile preferencias.yaml --answers respostas.yaml --no-llm --report-out relatorio.md`. Trate cada `not_found` como pendência: se o usuário corrigir o termo, ajuste a lista ou o perfil e rode `plan` de novo. `resolve` não processa `not_found`.
5. Antes de `apply`, confira em `run.json` que não restam itens `needs_user` ou `not_found` sem decisão explícita de pular, e que todos os `decided` correspondem à lista. Mostre o resumo das escolhas e o custo estimado. Execute `python -m orchestrator.main apply --list lista-compras.md --profile preferencias.yaml --no-llm --report-out relatorio.md` somente quando o usuário pedir para adicionar ao carrinho.
6. Deixe a janela aberta. O usuário revisa e finaliza.

O script Python cuida de busca, regras, cliques e conferência; Jev fornece decisões estruturadas em lote. Use seu raciocínio para entender a intenção da lista e revisar exceções, em uma sessão por corrida. Evite chamadas de modelo por item.

Login no Andorinha (perfil do Playwright, não o Chrome normal): `python -m orchestrator.browser --login`.
