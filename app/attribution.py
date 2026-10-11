"""Regra de atribuição de venda a criador. É o único lugar onde essa regra existe.

Ordem de prioridade:
  1. Cupom: o primeiro cupom do pedido (na ordem em que a loja enviou) que pertence
     a um criador decide a atribuição.
  2. UTM: se nenhum cupom pertence a criador, vale o criador dono do utm_campaign.
  3. Nenhum dos dois: a venda fica sem criador.

Desempate quando cupom e UTM apontam para criadores diferentes: vence o cupom.
O cupom foi digitado pelo comprador no checkout do próprio pedido; a UTM vem do
link de entrada da sessão, que pode ter sido compartilhado/repostado por terceiros
e não prova que aquele clique levou à compra. O conflito fica registrado
(conflict=True) para auditoria.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

RULE_COUPON = "coupon"
RULE_UTM = "utm"
RULE_NONE = "none"


@dataclass(frozen=True)
class Decision:
    creator_id: str | None
    rule: str
    coupon_code: str | None = None
    utm_campaign: str | None = None
    conflict: bool = False


def normalize_coupon(code: str) -> str:
    return code.strip().upper()


def normalize_campaign(campaign: str) -> str:
    return campaign.strip().lower()


def decide(
    coupon_codes: Sequence[str],
    utm_campaign: str | None,
    coupon_owners: Mapping[str, str],
    campaign_owners: Mapping[str, str],
) -> Decision:
    """coupon_owners/campaign_owners: código normalizado -> creator_id."""
    utm_creator = None
    campaign = normalize_campaign(utm_campaign) if utm_campaign else None
    if campaign:
        utm_creator = campaign_owners.get(campaign)

    for raw in coupon_codes:
        code = normalize_coupon(raw)
        creator = coupon_owners.get(code)
        if creator:
            return Decision(
                creator_id=creator,
                rule=RULE_COUPON,
                coupon_code=code,
                utm_campaign=campaign if utm_creator else None,
                conflict=utm_creator is not None and utm_creator != creator,
            )

    if utm_creator:
        return Decision(creator_id=utm_creator, rule=RULE_UTM, utm_campaign=campaign)

    return Decision(creator_id=None, rule=RULE_NONE)
