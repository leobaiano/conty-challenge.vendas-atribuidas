from tests.conftest import order, refund


def _maria_order(client, total="200.00"):
    client.post("/webhooks/orders", json=order(total=total, coupons=["MARIA10"]))


def test_estorno_total_zera_atribuicao_sem_apagar_historico(client):
    _maria_order(client)
    r = client.post("/webhooks/refunds", json=refund("r1", "1001", "200.00"))
    assert r.json()["status"] == "applied"

    o = client.get("/orders/1001").json()
    assert o["attribution"]["creator_id"] == "maria"       # quem gerou a venda continua registrado
    assert o["attribution"]["state"] == "reverted"
    assert o["attribution"]["credited"] == "0.00"
    assert [(l["kind"], l["amount"]) for l in o["ledger"]] == [("sale", "200.00"), ("refund", "-200.00")]
    assert client.get("/creators/maria").json()["net"] == "0.00"


def test_estorno_parcial(client):
    _maria_order(client)
    client.post("/webhooks/refunds", json=refund("r1", "1001", "50.00"))

    o = client.get("/orders/1001").json()
    assert o["refunded"] == "50.00"
    assert o["net"] == "150.00"
    assert o["attribution"]["state"] == "partially_reverted"
    s = client.get("/creators/maria").json()
    assert (s["gross"], s["refunded"], s["net"]) == ("200.00", "50.00", "150.00")


def test_varios_estornos_parciais_somam(client):
    _maria_order(client)
    client.post("/webhooks/refunds", json=refund("r1", "1001", "50.00"))
    client.post("/webhooks/refunds", json=refund("r2", "1001", "30.00", "20.00"))
    o = client.get("/orders/1001").json()
    assert o["refunded"] == "100.00"
    assert [r["id"] for r in o["refunds"]] == ["r1", "r2"]


def test_estorno_repetido_nao_desconta_de_novo(client):
    _maria_order(client)
    first = client.post("/webhooks/refunds", json=refund("r1", "1001", "50.00"))
    second = client.post("/webhooks/refunds", json=refund("r1", "1001", "50.00"))

    assert first.json()["status"] == "applied"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    o = client.get("/orders/1001").json()
    assert o["refunded"] == "50.00"
    assert len(o["refunds"]) == 1
    assert client.get("/creators/maria").json()["net"] == "150.00"


def test_soma_dos_estornos_nunca_passa_do_valor_da_venda(client):
    _maria_order(client)
    client.post("/webhooks/refunds", json=refund("r1", "1001", "150.00"))
    r2 = client.post("/webhooks/refunds", json=refund("r2", "1001", "100.00")).json()["refund"]
    r3 = client.post("/webhooks/refunds", json=refund("r3", "1001", "10.00")).json()["refund"]

    assert (r2["requested"], r2["applied"], r2["ignored_excess"]) == ("100.00", "50.00", "50.00")
    assert (r3["applied"], r3["ignored_excess"]) == ("0.00", "10.00")
    o = client.get("/orders/1001").json()
    assert o["refunded"] == "200.00"
    assert o["net"] == "0.00"
    assert client.get("/creators/maria").json()["net"] == "0.00"


def test_estorno_antes_do_pedido_fica_pendente_e_e_aplicado(client):
    r = client.post("/webhooks/refunds", json=refund("r1", "1001", "80.00"))
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    assert r.json()["refund"]["applied"] is None
    assert client.get("/orders/1001").status_code == 404

    _maria_order(client)

    o = client.get("/orders/1001").json()
    assert o["refunds"][0]["status"] == "applied"
    assert o["refunded"] == "80.00"
    assert [(l["kind"], l["amount"]) for l in o["ledger"]] == [("sale", "200.00"), ("refund", "-80.00")]
    assert client.get("/creators/maria").json()["net"] == "120.00"


def test_varios_estornos_antes_do_pedido_respeitam_o_teto(client):
    client.post("/webhooks/refunds", json=refund("r1", "1001", "150.00"))
    client.post("/webhooks/refunds", json=refund("r2", "1001", "150.00"))
    _maria_order(client)

    o = client.get("/orders/1001").json()
    assert [(r["id"], r["applied"]) for r in o["refunds"]] == [("r1", "150.00"), ("r2", "50.00")]
    assert o["net"] == "0.00"


def test_estorno_pendente_repetido_e_aplicado_uma_vez(client):
    client.post("/webhooks/refunds", json=refund("r1", "1001", "40.00"))
    dup = client.post("/webhooks/refunds", json=refund("r1", "1001", "40.00"))
    assert dup.json()["status"] == "duplicate"
    _maria_order(client)
    again = client.post("/webhooks/refunds", json=refund("r1", "1001", "40.00"))
    assert again.json()["status"] == "duplicate"

    assert client.get("/orders/1001").json()["refunded"] == "40.00"
    assert client.get("/creators/maria").json()["net"] == "160.00"


def test_pedido_repetido_depois_de_estorno_pendente_nao_reaplica(client):
    client.post("/webhooks/refunds", json=refund("r1", "1001", "40.00"))
    _maria_order(client)
    _maria_order(client)
    o = client.get("/orders/1001").json()
    assert o["refunded"] == "40.00"
    assert len(o["ledger"]) == 2


def test_estorno_de_pedido_sem_criador_fica_rastreado(client):
    client.post("/webhooks/orders", json=order())
    client.post("/webhooks/refunds", json=refund("r1", "1001", "20.00"))
    o = client.get("/orders/1001").json()
    assert o["refunded"] == "20.00"
    assert o["ledger"] == []


def test_estorno_ignora_transacoes_que_nao_sao_estorno_efetivado(client):
    _maria_order(client)
    payload = refund("r1", "1001", "30.00")
    payload["transactions"] += [
        {"kind": "refund", "status": "failure", "amount": "100.00"},
        {"kind": "sale", "status": "success", "amount": "100.00"},
    ]
    client.post("/webhooks/refunds", json=payload)
    assert client.get("/orders/1001").json()["refunded"] == "30.00"
