"""Status do pedido: só pedido pago gera crédito; cancelamento reverte sem apagar histórico."""

from tests.conftest import order, refund


def _maria(client, status, **extra):
    return client.post("/webhooks/orders", json=order(coupons=["MARIA10"], status=status, **extra)).json()


def _ledger(client):
    return [(l["kind"], l["amount"]) for l in client.get("/orders/1001").json()["ledger"]]


def test_pedido_pendente_nao_gera_credito(client):
    body = _maria(client, "pending")
    assert body["status"] == "created"
    attr = body["order"]["attribution"]
    assert attr["creator_id"] == "maria"          # atribuição já decidida
    assert attr["state"] == "awaiting_payment"
    assert attr["credited"] == "0.00"
    assert client.get("/creators/maria").json()["gross"] == "0.00"


def test_pendente_depois_pago_credita_uma_vez(client):
    _maria(client, "pending")
    body = _maria(client, "paid")
    assert body["status"] == "status_updated"
    assert body["order"]["attribution"]["state"] == "active"

    dup = _maria(client, "paid")
    assert dup["status"] == "duplicate"
    assert _ledger(client) == [("sale", "200.00")]
    assert client.get("/creators/maria").json()["net"] == "200.00"


def test_pagamento_nao_muda_atribuicao_decidida_na_chegada(client):
    client.post("/webhooks/orders", json=order(coupons=["MARIA10"], status="pending"))
    client.post("/webhooks/orders", json=order(coupons=["JOAO15"], total="999.00", status="paid"))
    o = client.get("/orders/1001").json()
    assert o["attribution"]["creator_id"] == "maria"
    assert o["total"] == "200.00"
    assert client.get("/creators/maria").json()["net"] == "200.00"


def test_status_atrasado_nao_volta_atras(client):
    _maria(client, "paid")
    body = _maria(client, "pending")
    assert body["status"] == "stale"
    assert body["order"]["financial_status"] == "paid"
    assert client.get("/creators/maria").json()["net"] == "200.00"


def test_cancelamento_depois_de_pago_reverte_sem_apagar(client):
    _maria(client, "paid")
    body = _maria(client, "voided")
    assert body["status"] == "status_updated"
    o = body["order"]
    assert o["stage"] == "cancelled"
    assert o["attribution"]["creator_id"] == "maria"
    assert o["attribution"]["state"] == "cancelled"
    assert _ledger(client) == [("sale", "200.00"), ("cancel", "-200.00")]
    s = client.get("/creators/maria").json()
    assert (s["gross"], s["cancelled"], s["net"]) == ("200.00", "200.00", "0.00")


def test_cancelamento_pelo_cancelled_at(client):
    _maria(client, "paid")
    _maria(client, "paid", cancelled_at="2026-10-10T12:00:00-03:00")
    assert client.get("/creators/maria").json()["net"] == "0.00"


def test_cancelamento_e_final(client):
    _maria(client, "paid")
    _maria(client, "voided")
    assert _maria(client, "paid")["status"] == "stale"
    assert _ledger(client) == [("sale", "200.00"), ("cancel", "-200.00")]


def test_cancelado_sem_ter_sido_pago_nunca_credita(client):
    _maria(client, "pending")
    _maria(client, "voided")
    _maria(client, "paid")                        # atrasado, ignorado
    assert _ledger(client) == []
    assert client.get("/orders/1001").json()["attribution"]["state"] == "cancelled"


def test_estorno_parcial_e_depois_cancelamento_cancela_so_o_saldo(client):
    _maria(client, "paid")
    client.post("/webhooks/refunds", json=refund("r1", "1001", "50.00"))
    _maria(client, "voided")
    assert _ledger(client) == [("sale", "200.00"), ("refund", "-50.00"), ("cancel", "-150.00")]
    assert client.get("/creators/maria").json()["net"] == "0.00"


def test_estorno_depois_do_cancelamento_nao_deixa_credito_negativo(client):
    _maria(client, "paid")
    _maria(client, "voided")
    r = client.post("/webhooks/refunds", json=refund("r1", "1001", "200.00")).json()
    assert r["refund"]["applied"] == "200.00"       # dinheiro do pedido continua rastreado
    assert _ledger(client) == [("sale", "200.00"), ("cancel", "-200.00")]
    assert client.get("/creators/maria").json()["net"] == "0.00"


def test_estorno_de_pedido_pendente_e_descontado_quando_paga(client):
    _maria(client, "pending")
    client.post("/webhooks/refunds", json=refund("r1", "1001", "30.00"))
    assert _ledger(client) == []
    _maria(client, "paid")
    assert _ledger(client) == [("sale", "200.00"), ("refund", "-30.00")]
    assert client.get("/creators/maria").json()["net"] == "170.00"


def test_estorno_antes_de_pedido_pendente_e_descontado_quando_paga(client):
    client.post("/webhooks/refunds", json=refund("r1", "1001", "30.00"))
    _maria(client, "pending")
    _maria(client, "paid")
    assert _ledger(client) == [("sale", "200.00"), ("refund", "-30.00")]


def test_status_desconhecido_e_rejeitado(client):
    r = client.post("/webhooks/orders", json=order(status="foo"))
    assert r.status_code == 422
    assert client.get("/orders/1001").status_code == 404


def test_status_obrigatorio(client):
    payload = order()
    del payload["financial_status"]
    assert client.post("/webhooks/orders", json=payload).status_code == 422
