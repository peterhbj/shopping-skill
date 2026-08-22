# Dúvidas da busca — não foram para o carrinho

Responda cada item (marca, tamanho, pular…). Com as respostas:
`python -m orchestrator.main resolve --list lista-compras.md --profile preferencias.yaml --answers respostas.yaml`

## 1. Carne de panela (2)

- **Busca**: `acém 0`
- **Qty**: 2 kg
- **Por quê**: Filtro frouxo e mais de um resultado — não chutar · índice LLM inválido
- **O que apareceu**:
  - [0] Acém Prata Pedaço Kg — R$37.99
  - [1] Acém Prata Especial Kg — R$47.99
  - [2] Miolo Acém Ouro  Kg — R$59.90

## 2. Bife

- **Busca**: `patinho 0`
- **Qty**: 1.8 kg
- **Por quê**: Filtro frouxo e mais de um resultado — não chutar · índice LLM inválido
- **O que apareceu**:
  - [0] Patinho Prata Moída Kg — R$49.90
  - [1] Patinho Prata Fatiado Kg — R$49.90
  - [2] Patinho Prata Pedaço Kg — R$49.90
  - [3] Patinho Ouro Fatiado Kg — R$59.90
  - [4] Patinho Ouro Pedaço Kg — R$59.90
  - [5] Carne Moída Patinho Ouro Kg — R$64.90

Exemplo `respostas.yaml`:

```yaml
respostas:
  - item: Carne de panela (2)
    texto: pular
```
