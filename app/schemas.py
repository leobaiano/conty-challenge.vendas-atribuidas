"""Payloads aceitos. Seguem o essencial dos webhooks orders/paid e refunds/create
da Shopify; campos extras são ignorados."""

from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.money import to_cents
from app.order_status import FINANCIAL_STATUS_STAGE


class DiscountCode(BaseModel):
    model_config = ConfigDict(extra="ignore")
    code: str


class OrderPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    total_price: str
    currency: str = "BRL"
    financial_status: str
    cancelled_at: str | None = None
    discount_codes: list[DiscountCode] = Field(default_factory=list)
    landing_site: str | None = None
    created_at: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _id_as_str(cls, v):
        return str(v)

    @field_validator("financial_status")
    @classmethod
    def _known_status(cls, v):
        if v not in FINANCIAL_STATUS_STAGE:
            raise ValueError(f"financial_status desconhecido: {v!r}")
        return v

    @field_validator("total_price", mode="before")
    @classmethod
    def _valid_total(cls, v):
        v = str(v)
        if to_cents(v) < 0:
            raise ValueError("total_price não pode ser negativo")
        return v

    @property
    def total_cents(self) -> int:
        return to_cents(self.total_price)

    @property
    def utms(self) -> dict[str, str | None]:
        query = parse_qs(urlsplit(self.landing_site or "").query)
        keys = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
        return {k: (query[k][0] if k in query else None) for k in keys}


class RefundTransaction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str = "refund"
    status: str = "success"
    amount: str

    @field_validator("amount", mode="before")
    @classmethod
    def _valid_amount(cls, v):
        v = str(v)
        if to_cents(v) < 0:
            raise ValueError("amount não pode ser negativo")
        return v


class RefundPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    order_id: str
    created_at: str | None = None
    transactions: list[RefundTransaction] = Field(default_factory=list)

    @field_validator("id", "order_id", mode="before")
    @classmethod
    def _as_str(cls, v):
        return str(v)

    @property
    def amount_cents(self) -> int:
        return sum(
            to_cents(t.amount)
            for t in self.transactions
            if t.kind == "refund" and t.status == "success"
        )


class CreatorIn(BaseModel):
    id: str
    name: str
    coupon_codes: list[str] = Field(default_factory=list)
    utm_campaigns: list[str] = Field(default_factory=list)
