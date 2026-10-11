import os

from fastapi import FastAPI, Header, HTTPException

from app import services
from app.db import connect, init_db
from app.schemas import CreatorIn, OrderPayload, RefundPayload


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("DATABASE_PATH", "vendas.db")
    init_db(db_path)
    app = FastAPI(title="Vendas atribuídas")

    def conn():
        return connect(db_path)

    @app.post("/creators", status_code=201)
    def create_creator(data: CreatorIn):
        c = conn()
        try:
            services.create_creator(c, data)
            return services.get_creator_summary(c, data.id)
        except services.Conflict as e:
            raise HTTPException(409, str(e))
        finally:
            c.close()

    @app.get("/creators/{creator_id}")
    def creator_summary(creator_id: str):
        c = conn()
        try:
            summary = services.get_creator_summary(c, creator_id)
        finally:
            c.close()
        if not summary:
            raise HTTPException(404, "criador não encontrado")
        return summary

    # Webhooks sempre respondem 200 para entregas válidas (inclusive duplicadas),
    # senão a loja reenvia.
    @app.post("/webhooks/orders")
    def order_webhook(payload: OrderPayload, x_shopify_webhook_id: str | None = Header(None)):
        c = conn()
        try:
            return services.ingest_order(c, payload, x_shopify_webhook_id)
        finally:
            c.close()

    @app.post("/webhooks/refunds")
    def refund_webhook(payload: RefundPayload, x_shopify_webhook_id: str | None = Header(None)):
        c = conn()
        try:
            return services.ingest_refund(c, payload, x_shopify_webhook_id)
        finally:
            c.close()

    @app.get("/orders/{order_id}")
    def order_detail(order_id: str):
        c = conn()
        try:
            order = services.get_order(c, order_id)
        finally:
            c.close()
        if not order:
            raise HTTPException(404, "pedido não encontrado")
        return order

    return app

