import threading

from app import services
from app.db import connect
from app.schemas import OrderPayload
from tests.conftest import order


def test_pedido_atribuido_por_cupom(client):
    r = client.post("/webhooks/orders", json=order(coupons=["MARIA10"]))
    assert r.status_code == 200
    assert r.json()["status"] == "created"
    assert client.get("/creators/maria").json() == {
        "creator_id": "maria", "name": "Maria", "orders": 1,
        "gross": "200.00", "refunded": "0.00", "cancelled": "0.00", "net": "200.00",
    }


def test_utm_lida_do_landing_site(client):
    r = client.post("/webhooks/orders", json=order(landing_site="/?utm_source=tiktok&utm_campaign=joao"))
    o = r.json()["order"]
    assert o["utm"]["utm_source"] == "tiktok"
    assert o["attribution"]["creator_id"] == "joao"
    assert o["attribution"]["rule"] == "utm"


def test_pedido_duplicado_nao_soma_de_novo(client):
    payload = order(coupons=["MARIA10"])
    first = client.post("/webhooks/orders", json=payload, headers={"X-Shopify-Webhook-Id": "a"})
    second = client.post("/webhooks/orders", json=payload, headers={"X-Shopify-Webhook-Id": "b"})

    assert first.json()["status"] == "created"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    summary = client.get("/creators/maria").json()
    assert summary["orders"] == 1
    assert summary["gross"] == "200.00"
    assert len(client.get("/orders/1001").json()["ledger"]) == 1


def test_reenvio_com_dados_diferentes_nao_muda_atribuicao(client):
    client.post("/webhooks/orders", json=order(coupons=["MARIA10"]))
    client.post("/webhooks/orders", json=order(total="999.00", coupons=["JOAO15"]))
    o = client.get("/orders/1001").json()
    assert o["total"] == "200.00"
    assert o["attribution"]["creator_id"] == "maria"
    assert client.get("/creators/joao").json()["gross"] == "0.00"


def test_pedido_duplicado_concorrente(client, db_path):
    payload = OrderPayload(**order(coupons=["MARIA10"]))
    results = []

    def send():
        c = connect(db_path)
        try:
            results.append(services.ingest_order(c, payload)["status"])
        finally:
            c.close()

    threads = [threading.Thread(target=send) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["created"] + ["duplicate"] * 7
    assert client.get("/creators/maria").json()["gross"] == "200.00"


def test_todas_as_entregas_ficam_registradas(client, db_path):
    for _ in range(3):
        client.post("/webhooks/orders", json=order(coupons=["MARIA10"]))
    c = connect(db_path)
    outcomes = [r["outcome"] for r in c.execute("SELECT outcome FROM webhook_deliveries ORDER BY id")]
    c.close()
    assert outcomes == ["created", "duplicate", "duplicate"]


def test_pedido_sem_criador(client):
    o = client.post("/webhooks/orders", json=order(coupons=["QUALQUER"])).json()["order"]
    assert o["attribution"]["creator_id"] is None
    assert o["attribution"]["state"] == "unattributed"
    assert o["ledger"] == []


def test_valor_invalido_rejeitado(client):
    assert client.post("/webhooks/orders", json=order(total="10.999")).status_code == 422
    assert client.post("/webhooks/orders", json=order(total="-1.00")).status_code == 422
    assert client.get("/orders/1001").status_code == 404
