"""Ciclo de vida do pedido a partir do financial_status da Shopify.

Estágios, em ordem: UNPAID -> PAID -> CANCELLED. Um pedido só avança de estágio;
entrega atrasada com estágio anterior (ex.: "pending" chegando depois de "paid")
é ignorada. CANCELLED é final.

Só pedidos que chegaram a PAID geram crédito para o criador. Os estornos
(refunds/create) continuam sendo o caminho para descontos parciais; status como
"partially_refunded" e "refunded" contam como PAID porque o dinheiro entrou.
"""

UNPAID = "unpaid"
PAID = "paid"
CANCELLED = "cancelled"

RANK = {UNPAID: 0, PAID: 1, CANCELLED: 2}

FINANCIAL_STATUS_STAGE = {
    "pending": UNPAID,
    "authorized": UNPAID,
    "partially_paid": UNPAID,
    "paid": PAID,
    "partially_refunded": PAID,
    "refunded": PAID,
    "voided": CANCELLED,
    "expired": CANCELLED,
}


def stage(financial_status: str, cancelled_at: str | None) -> str:
    if cancelled_at:
        return CANCELLED
    return FINANCIAL_STATUS_STAGE[financial_status]


def advances(current: str, new: str) -> bool:
    return RANK[new] > RANK[current]
