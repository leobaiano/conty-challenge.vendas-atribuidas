import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def client(db_path):
    c = TestClient(create_app(db_path))
    c.post("/creators", json={"id": "maria", "name": "Maria", "coupon_codes": ["MARIA10"], "utm_campaigns": ["maria"]})
    c.post("/creators", json={"id": "joao", "name": "João", "coupon_codes": ["JOAO15"], "utm_campaigns": ["joao"]})
    return c


def order(order_id="1001", total="200.00", coupons=(), landing_site=None, status="paid", **extra):
    return {
        "id": order_id,
        "total_price": total,
        "currency": "BRL",
        "financial_status": status,
        "discount_codes": [{"code": c, "amount": "10.00", "type": "percentage"} for c in coupons],
        "landing_site": landing_site,
        **extra,
    }


def refund(refund_id, order_id="1001", *amounts):
    return {
        "id": refund_id,
        "order_id": order_id,
        "transactions": [{"kind": "refund", "status": "success", "amount": a} for a in amounts],
    }
