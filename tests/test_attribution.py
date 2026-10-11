"""Testes da regra de atribuição (app/attribution.py)."""

from app.attribution import RULE_COUPON, RULE_NONE, RULE_UTM, decide
from tests.conftest import order

COUPONS = {"MARIA10": "maria", "JOAO15": "joao"}
CAMPAIGNS = {"maria": "maria", "joao": "joao"}


def test_somente_cupom():
    d = decide(["MARIA10"], None, COUPONS, CAMPAIGNS)
    assert (d.creator_id, d.rule, d.conflict) == ("maria", RULE_COUPON, False)


def test_somente_utm():
    d = decide([], "joao", COUPONS, CAMPAIGNS)
    assert (d.creator_id, d.rule) == ("joao", RULE_UTM)


def test_cupom_e_utm_do_mesmo_criador_sem_conflito():
    d = decide(["MARIA10"], "maria", COUPONS, CAMPAIGNS)
    assert (d.creator_id, d.rule, d.conflict) == ("maria", RULE_COUPON, False)


def test_desempate_cupom_vence_utm_de_outro_criador():
    d = decide(["MARIA10"], "joao", COUPONS, CAMPAIGNS)
    assert d.creator_id == "maria"
    assert d.rule == RULE_COUPON
    assert d.conflict is True
    assert d.utm_campaign == "joao"


def test_cupom_que_nao_e_de_criador_cai_para_utm():
    d = decide(["BLACKFRIDAY"], "joao", COUPONS, CAMPAIGNS)
    assert (d.creator_id, d.rule) == ("joao", RULE_UTM)


def test_varios_cupons_de_criadores_vale_o_primeiro():
    d = decide(["JOAO15", "MARIA10"], None, COUPONS, CAMPAIGNS)
    assert d.creator_id == "joao"


def test_normaliza_maiusculas_e_espacos():
    d = decide([" maria10 "], " MARIA ", COUPONS, CAMPAIGNS)
    assert (d.creator_id, d.coupon_code, d.conflict) == ("maria", "MARIA10", False)


def test_sem_cupom_nem_utm_conhecidos():
    d = decide(["OUTRO"], "desconhecida", COUPONS, CAMPAIGNS)
    assert (d.creator_id, d.rule) == (None, RULE_NONE)


def test_desempate_via_webhook(client):
    body = client.post(
        "/webhooks/orders",
        json=order(coupons=["MARIA10"], landing_site="/p/tenis?utm_source=instagram&utm_campaign=joao"),
    ).json()
    attr = body["order"]["attribution"]
    assert attr["creator_id"] == "maria"
    assert attr["conflict"] is True
    assert client.get("/creators/maria").json()["net"] == "200.00"
    assert client.get("/creators/joao").json()["net"] == "0.00"
