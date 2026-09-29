---
name: andorinha-shopping
description: Planejar e revisar compras do Andorinha com a lista, preferencias.yaml, Jev e o orquestrador Python deste repositório. Use ao pedir para analisar a lista, comparar produtos, preparar o carrinho ou executar uma compra assistida.
---

# Compras assistidas

Trabalhe na raiz deste repositório. Use o Codex como coordenador de uma corrida, não como subprocesso para cada produto. Leia `lista-compras.md`, `preferencias.yaml` e o `README.md` antes de planejar. Não exiba credenciais, dados de autenticação ou arquivos de perfil do navegador.

1. Confira a lista e as preferências. Aponte apenas inconsistências que afetem a escolha; preserve quantidades, sabores, restrições e marcas explicitamente pedidas.
2. Execute `python -m orchestrator.main plan --list lista-compras.md --profile preferencias.yaml --no-llm --jev-shadow --report-out relatorio.md` se `TYPESAFE_API_KEY` estiver configurada. Sem a chave, use `--no-jev` no lugar de `--jev-shadow`. Não leia nem imprima o valor da chave.
3. Leia `run.json`, `relatorio.md` e `duvidas.md` se existir. Revise também as escolhas automáticas feitas pelo modo `--no-llm`: marca ou menor preço podem não corresponder ao pedido. Compare as sugestões do Jev com os candidatos e o perfil. Jev em modo shadow apenas registra sugestões; uma confiança alta não garante que o produto corresponda à lista. Não escolha candidatos fora dos resultados pesquisados.
4. Para dúvidas, explique opções e diferenças de preço/unidade ao usuário. Quando houver informação suficiente ou uma escolha expressa, prepare um arquivo local `respostas.yaml` no formato `respostas: [{item: "texto do item", texto: "índice do candidato"}]` e execute `python -m orchestrator.main resolve --list lista-compras.md --profile preferencias.yaml --answers respostas.yaml --no-llm --report-out relatorio.md`. Não invente respostas para restrições, variações de produto ou substituições relevantes.
5. Antes de `apply`, confira em `run.json` que não restam itens `needs_user` e que todos os itens `decided` correspondem à lista. Mostre o resumo das escolhas e custo estimado. Execute `python -m orchestrator.main apply --list lista-compras.md --profile preferencias.yaml --no-llm --report-out relatorio.md` somente quando o usuário pedir para adicionar os itens ao carrinho. Checkout e pagamento são manuais.

O script Python cuida de busca, regras, cliques e conferência; Jev fornece decisões estruturadas em lote. Use seu raciocínio para entender a intenção da lista e revisar exceções, em uma sessão por corrida. Evite iniciar `codex exec` de dentro da própria sessão e evite chamadas de modelo por item.
