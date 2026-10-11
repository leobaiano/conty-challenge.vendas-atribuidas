# Vendas atribuídas

Recebe webhooks de pedido e de estorno (no formato essencial da Shopify), atribui cada venda a no máximo um criador e mantém o histórico de estornos sem apagar nada.

Stack: Python 3.10+, FastAPI e SQLite (stdlib). Testes com pytest.

## Rodando

```bash
uv sync
uv run uvicorn app.main:create_app --factory --reload   # API em http://localhost:8000 (docs em /docs)
uv run pytest -v                                          # testes
```

O banco fica em `vendas.db`. Para usar outro arquivo, defina `DATABASE_PATH`.

## Endpoints

| Método | Rota | O que faz |
|---|---|---|
| POST | `/creators` | Cadastra criador com seus cupons e `utm_campaign`s |
| GET | `/creators/{id}` | Bruto, estornado e líquido atribuído ao criador |
| POST | `/webhooks/orders` | Ingestão de pedido (tópicos `orders/create`, `orders/paid`, `orders/updated`, `orders/cancelled`) |
| POST | `/webhooks/refunds` | Ingestão de estorno (tópico `refunds/create`) |
| GET | `/orders/{id}` | Pedido, atribuição, estornos e livro-razão |

### Payloads

Pedido (campos extras são ignorados):

```json
{
  "id": 820982911946154508,
  "total_price": "199.90",
  "currency": "BRL",
  "financial_status": "paid",
  "discount_codes": [{ "code": "MARIA10" }],
  "landing_site": "/produtos/tenis?utm_source=instagram&utm_campaign=maria"
}
```

As UTMs são lidas da query string de `landing_site`, como a Shopify envia. `financial_status` é obrigatório e precisa ser um status conhecido. `cancelled_at` é opcional e, quando vem preenchido, o pedido é tratado como cancelado.

Estorno:

```json
{
  "id": 509562969,
  "order_id": 820982911946154508,
  "transactions": [{ "kind": "refund", "status": "success", "amount": "50.00" }]
}
```

O valor do estorno é a soma das transações com `kind = "refund"` e `status = "success"`.

O cabeçalho `X-Shopify-Webhook-Id` é opcional e fica gravado no log de entregas.

Entregas válidas sempre recebem `200`, inclusive as duplicadas (`"status": "duplicate"`). Se a resposta fosse de erro, a loja reenviaria o webhook.

## Regras

### Atribuição: fica só em `app/attribution.py`

1. **Cupom**: vale o primeiro cupom do pedido que pertence a um criador.
2. **UTM**: se nenhum cupom é de criador, vale o dono do `utm_campaign`.
3. Se nenhum dos dois casa, a venda fica sem criador (`rule = "none"`).

**Desempate (cupom e UTM apontam para criadores diferentes): o cupom vence.** O comprador digitou o cupom no checkout desse pedido, então a ligação com a compra é direta. Já a UTM vem do link de entrada da sessão: o link pode ter sido repassado por outra pessoa e não prova que aquele clique levou à compra. O conflito não some: fica gravado em `attribution.conflict = true`, com a `utm_campaign` perdedora, para auditoria.

Cupom e campanha não diferenciam maiúsculas de minúsculas. Cada cupom ou campanha pertence a um único criador (o cadastro rejeita duplicata com 409).

A decisão é tomada uma vez, quando o pedido chega pela primeira vez (mesmo sem estar pago), e não é recalculada depois.

### Status do pedido: fica em `app/order_status.py`

O `financial_status` da Shopify é traduzido em três estágios. Um pedido só avança de estágio, nunca volta:

| Estágio | `financial_status` | Crédito do criador |
|---|---|---|
| `unpaid` | `pending`, `authorized`, `partially_paid` | Nenhum. A atribuição já está decidida, mas fica em `awaiting_payment` |
| `paid` | `paid`, `partially_refunded`, `refunded` | A venda é lançada uma vez (+total) |
| `cancelled` | `voided`, `expired` ou `cancelled_at` preenchido | O saldo que ainda resta é revertido com um lançamento `cancel` |

- **Pendente e depois pago:** o crédito é lançado quando o pagamento chega, e uma única vez. Estornos já aplicados ao pedido são descontados nesse momento.
- **Entrega atrasada** (por exemplo, `pending` chegando depois de `paid`): fica registrada com `outcome = stale` e não muda nada.
- **Cancelado depois de pago:** um lançamento `cancel` zera o saldo restante. A venda e os estornos anteriores continuam no livro-razão. Estornos que chegam depois do cancelamento ficam registrados no pedido, mas não geram lançamento, então o crédito nunca fica negativo.
- **Cancelado sem nunca ter sido pago:** nunca gera crédito, mesmo que um `paid` atrasado chegue depois. O cancelamento é final.
- `partially_refunded` e `refunded` contam como pagos porque o dinheiro entrou. Os descontos vêm dos webhooks de estorno, que trazem o valor de cada estorno. Assim o mesmo estorno não é descontado duas vezes, uma pelo status e outra pelo webhook.

O livro-razão do pedido é calculado a partir do status e dos estornos por uma única função (`_sync_ledger`). Ela só insere linhas, nunca altera nem apaga, e pode ser chamada de novo sem gerar lançamento repetido.

### Idempotência

- **Pedido**: a chave é o `id` do pedido. Se o mesmo pedido chegar de novo, mesmo com dados diferentes, ele não altera total, cupons, UTMs nem atribuição. O único efeito possível é o status avançar (`outcome = status_updated`). Se o status for o mesmo, a entrega é só registrada (`duplicate`). Se for um status anterior, é ignorada (`stale`). Todas as entregas ficam em `webhook_deliveries`. Há também uma restrição `UNIQUE` que permite uma única linha de venda e uma única de cancelamento por pedido no livro-razão.
- **Estorno**: a chave é o `id` do estorno. A repetição é registrada e ignorada.
- As transações usam `BEGIN IMMEDIATE`, que serializa as escritas. Também há restrições `UNIQUE` (pedido, estorno e uma única venda por pedido no livro-razão). Assim, entregas duplicadas simultâneas não somam duas vezes, e há teste com 8 threads para isso.

### Estornos

- O estorno **não apaga** nada. A atribuição continua apontando para o criador, e cada estorno vira uma linha negativa em `attribution_ledger`. O crédito do criador é a soma do livro-razão. O estado da atribuição é `awaiting_payment`, `active`, `partially_reverted`, `reverted` ou `cancelled`.
- **Teto**: o valor aplicado é `min(valor solicitado no estorno, total do pedido − já estornado)`. Por isso a soma dos estornos nunca passa do valor da venda. O excedente não é descartado em silêncio: cada estorno mostra `requested`, `applied` e `ignored_excess`.
- **Estorno antes do pedido**: fica gravado com `status = pending`, sem FK para o pedido. Quando o pedido chega, os pendentes são aplicados na mesma transação, na ordem de chegada, respeitando o teto.
- Um estorno de pedido sem criador também fica registrado, só não gera linha no livro-razão.

Valores monetários são guardados em centavos inteiros. Valor com mais de duas casas decimais ou negativo é rejeitado com 422.

## Testes

- `tests/test_attribution.py`: regra de atribuição, incluindo o desempate cupom × UTM (direto na função e via webhook)
- `tests/test_orders.py`: pedido duplicado (sequencial, com dados diferentes e concorrente) e log de entregas
- `tests/test_order_status.py`: pedido pendente sem crédito, pendente → pago (crédito uma vez só), status atrasado, cancelamento depois de pago (total e após estorno parcial), cancelamento sem pagamento, estorno depois do cancelamento e status inválido
- `tests/test_refunds.py`: estorno total, parcial, vários parciais, repetido, teto da soma, estorno antes do pedido (simples, múltiplo e repetido) e pedido repetido depois de estorno pendente

## Fora do escopo / limitações conhecidas

- Não há validação de HMAC (`X-Shopify-Hmac-Sha256`). Em produção ela seria obrigatória.
- O status não muda a atribuição: o criador é decidido na primeira entrega do pedido. Um pedido editado depois (troca de cupom, por exemplo) não é reatribuído.
- `partially_paid` (pagamento em partes) conta como não pago até chegar `paid`. Não há crédito proporcional.
- Moeda: o total é guardado com a moeda do pedido, mas não há conversão.
- Os campos `customer`, `line_items` e outros da Shopify não são usados.
- SQLite atende ao desafio. Com vários processos escrevendo, o caminho seria Postgres com as mesmas restrições `UNIQUE` e `SELECT ... FOR UPDATE` no pedido.

## Uso de IA

O código, os testes e este README foram gerados com o Claude Code (modelo Claude Opus 5.5), a partir do enunciado do desafio. A IA escolheu a stack, modelou as tabelas (livro-razão só de inserção, estornos pendentes sem FK, teto de estorno), definiu a regra de desempate (cupom vence UTM) e escreveu todos os testes.

Numa segunda rodada, também com IA, foi implementado o tratamento de `financial_status`. Antes, o status era guardado mas ignorado, então um pedido `pending` ou `voided` era creditado como venda.

**O que eu revisei:**

- Validei a conversão de valores para centavos inteiros e a regra do teto de estornos (`min(valor solicitado, total − já estornado)`), garantindo que a soma dos estornos nunca ultrapasse o valor da venda e que o crédito do criador nunca fique negativo, inclusive com estorno chegando depois de cancelamento.
- Revisei a função `_sync_ledger` para confirmar que o livro-razão (`attribution_ledger`) só recebe inserções, sem UPDATE nem DELETE, e que pode ser chamada de novo sem duplicar lançamentos. Isso é garantido pelo código, não por trigger no banco. A tabela `refunds` recebe um único UPDATE, quando o estorno passa de `pending` para `applied`; o valor solicitado e a data de chegada não mudam.
- Inspecionei o uso de transações `BEGIN IMMEDIATE` no SQLite e das restrições UNIQUE, e o teste com 8 threads enviando o mesmo pedido ao mesmo tempo, que confirma que a venda é somada uma única vez. Estornos simultâneos usam o mesmo mecanismo, mas não têm teste de concorrência próprio.
- Conferi a lógica de estornos que chegam antes do pedido: ficam pendentes e são aplicados, na ordem de chegada e respeitando o teto, na mesma transação em que o pedido é gravado.
- Validei a prioridade do cupom sobre a UTM e a marcação do campo de auditoria `attribution.conflict`, que só é ligado quando cupom e UTM pertencem a criadores diferentes.
- Revisei as regras de transição de `financial_status`: o estágio só avança (não pago → pago → cancelado), entrega atrasada é ignorada, e o cancelamento reverte só o saldo restante. Os status `partially_refunded` e `refunded` não mexem no saldo; os descontos vêm apenas dos webhooks de estorno, para não descontar o mesmo valor duas vezes.
