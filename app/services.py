import json
import sqlite3
from datetime import datetime, timezone

from app import attribution, order_status
from app.db import transaction
from app.money import format_cents
from app.schemas import CreatorIn, OrderPayload, RefundPayload


class Conflict(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_delivery(conn, topic, resource_id, delivery_id, outcome, payload: dict) -> None:
    conn.execute(
        "INSERT INTO webhook_deliveries (topic, resource_id, delivery_id, outcome, payload, received_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (topic, resource_id, delivery_id, outcome, json.dumps(payload), _now()),
    )


# --- criadores -------------------------------------------------------------

def create_creator(conn: sqlite3.Connection, data: CreatorIn) -> None:
    with transaction(conn):
        if conn.execute("SELECT 1 FROM creators WHERE id = ?", (data.id,)).fetchone():
            raise Conflict(f"criador {data.id} já existe")
        conn.execute("INSERT INTO creators (id, name) VALUES (?, ?)", (data.id, data.name))
        for code in data.coupon_codes:
            try:
                conn.execute(
                    "INSERT INTO creator_coupons (code, creator_id) VALUES (?, ?)",
                    (attribution.normalize_coupon(code), data.id),
                )
            except sqlite3.IntegrityError:
                raise Conflict(f"cupom {code} já pertence a outro criador")
        for campaign in data.utm_campaigns:
            try:
                conn.execute(
                    "INSERT INTO creator_utm_campaigns (campaign, creator_id) VALUES (?, ?)",
                    (attribution.normalize_campaign(campaign), data.id),
                )
            except sqlite3.IntegrityError:
                raise Conflict(f"utm_campaign {campaign} já pertence a outro criador")


# --- pedidos ---------------------------------------------------------------

def _decide(conn, coupon_codes: list[str], utm_campaign: str | None) -> attribution.Decision:
    codes = [attribution.normalize_coupon(c) for c in coupon_codes]
    coupon_owners = {}
    if codes:
        marks = ",".join("?" * len(codes))
        coupon_owners = {
            r["code"]: r["creator_id"]
            for r in conn.execute(
                f"SELECT code, creator_id FROM creator_coupons WHERE code IN ({marks})", codes
            )
        }
    campaign_owners = {}
    if utm_campaign:
        row = conn.execute(
            "SELECT campaign, creator_id FROM creator_utm_campaigns WHERE campaign = ?",
            (attribution.normalize_campaign(utm_campaign),),
        ).fetchone()
        if row:
            campaign_owners[row["campaign"]] = row["creator_id"]
    return attribution.decide(coupon_codes, utm_campaign, coupon_owners, campaign_owners)


def ingest_order(conn: sqlite3.Connection, payload: OrderPayload, delivery_id: str | None = None) -> dict:
    raw = payload.model_dump()
    new_stage = order_status.stage(payload.financial_status, payload.cancelled_at)
    with transaction(conn):
        existing = conn.execute("SELECT * FROM orders WHERE id = ?", (payload.id,)).fetchone()
        if existing:
            # Reenvio: total, cupons, UTMs e atribuição nunca mudam. Só o status pode
            # avançar (ex.: pending -> paid, paid -> voided).
            current = order_status.stage(existing["financial_status"], existing["cancelled_at"])
            if order_status.advances(current, new_stage):
                conn.execute(
                    "UPDATE orders SET financial_status = ?, cancelled_at = ? WHERE id = ?",
                    (payload.financial_status, payload.cancelled_at, payload.id),
                )
                _sync_ledger(conn, payload.id)
                outcome = "status_updated"
            elif new_stage == current:
                outcome = "duplicate"
            else:
                outcome = "stale"
            _log_delivery(conn, "orders", payload.id, delivery_id, outcome, raw)
            return {"status": outcome, "order": get_order(conn, payload.id)}

        now = _now()
        utms = payload.utms
        codes = [d.code for d in payload.discount_codes]
        conn.execute(
            "INSERT INTO orders (id, total_cents, currency, financial_status, cancelled_at, coupon_codes,"
            " utm_source, utm_medium, utm_campaign, utm_content, utm_term, created_at, received_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payload.id, payload.total_cents, payload.currency, payload.financial_status,
                payload.cancelled_at, json.dumps(codes), utms["utm_source"], utms["utm_medium"],
                utms["utm_campaign"], utms["utm_content"], utms["utm_term"], payload.created_at, now,
            ),
        )

        # A atribuição é decidida já na chegada, mesmo sem pagamento, para não
        # depender de cadastro de cupom/UTM que possa mudar até o pagamento.
        decision = _decide(conn, codes, utms["utm_campaign"])
        conn.execute(
            "INSERT INTO attributions (order_id, creator_id, rule, coupon_code, utm_campaign, conflict, decided_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (payload.id, decision.creator_id, decision.rule, decision.coupon_code,
             decision.utm_campaign, int(decision.conflict), now),
        )

        # Estornos que chegaram antes do pedido, na ordem em que chegaram.
        pending = conn.execute(
            "SELECT id FROM refunds WHERE order_id = ? AND status = 'pending' ORDER BY seq",
            (payload.id,),
        ).fetchall()
        for r in pending:
            _apply_refund(conn, r["id"])

        _sync_ledger(conn, payload.id)
        _log_delivery(conn, "orders", payload.id, delivery_id, "created", raw)
        return {"status": "created", "order": get_order(conn, payload.id)}


# --- crédito do criador ----------------------------------------------------

def _sync_ledger(conn, order_id: str) -> None:
    """Leva o livro-razão do pedido ao estado que o status e os estornos pedem.

    Só insere linhas, nunca altera ou apaga, e é idempotente: chamar de novo sem
    mudança no pedido não gera nada.
      - pedido pago e sem venda lançada  -> lança a venda (+total)
      - estorno aplicado sem lançamento  -> lança o estorno (-valor aplicado)
      - pedido cancelado com saldo > 0   -> lança o cancelamento (-saldo)
    Depois do cancelamento nada mais é lançado, então o crédito nunca fica negativo.
    """
    o = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    creator = conn.execute(
        "SELECT creator_id FROM attributions WHERE order_id = ?", (order_id,)
    ).fetchone()["creator_id"]
    if not creator:
        return
    stage = order_status.stage(o["financial_status"], o["cancelled_at"])
    kinds = {r["kind"] for r in conn.execute(
        "SELECT DISTINCT kind FROM attribution_ledger WHERE order_id = ?", (order_id,)
    )}
    if "cancel" in kinds:
        return

    now = _now()
    if "sale" not in kinds:
        if stage != order_status.PAID:
            return  # aguardando pagamento, ou cancelado sem nunca ter sido pago
        conn.execute(
            "INSERT INTO attribution_ledger (order_id, creator_id, kind, amount_cents, created_at)"
            " VALUES (?, ?, 'sale', ?, ?)",
            (order_id, creator, o["total_cents"], now),
        )

    missing = conn.execute(
        "SELECT r.id, r.applied_cents FROM refunds r"
        " LEFT JOIN attribution_ledger l ON l.refund_id = r.id"
        " WHERE r.order_id = ? AND r.status = 'applied' AND r.applied_cents > 0 AND l.id IS NULL"
        " ORDER BY r.seq",
        (order_id,),
    ).fetchall()
    for r in missing:
        conn.execute(
            "INSERT INTO attribution_ledger (order_id, creator_id, kind, amount_cents, refund_id, created_at)"
            " VALUES (?, ?, 'refund', ?, ?, ?)",
            (order_id, creator, -r["applied_cents"], r["id"], now),
        )

    if stage == order_status.CANCELLED:
        balance = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM attribution_ledger WHERE order_id = ?",
            (order_id,),
        ).fetchone()[0]
        if balance > 0:
            conn.execute(
                "INSERT INTO attribution_ledger (order_id, creator_id, kind, amount_cents, created_at)"
                " VALUES (?, ?, 'cancel', ?, ?)",
                (order_id, creator, -balance, now),
            )


# --- estornos --------------------------------------------------------------

def _apply_refund(conn, refund_id: str) -> None:
    """Aplica um estorno pendente cujo pedido já existe.

    O valor aplicado é limitado ao saldo ainda não estornado do pedido, então a
    soma dos estornos nunca passa do total. O excedente fica visível como
    requested_cents - applied_cents. O reflexo no crédito do criador é feito por
    _sync_ledger.
    """
    refund = conn.execute("SELECT * FROM refunds WHERE id = ?", (refund_id,)).fetchone()
    order = conn.execute("SELECT total_cents FROM orders WHERE id = ?", (refund["order_id"],)).fetchone()
    already = conn.execute(
        "SELECT COALESCE(SUM(applied_cents), 0) FROM refunds WHERE order_id = ? AND status = 'applied'",
        (refund["order_id"],),
    ).fetchone()[0]
    applied = max(0, min(refund["requested_cents"], order["total_cents"] - already))
    conn.execute(
        "UPDATE refunds SET status = 'applied', applied_cents = ?, applied_at = ? WHERE id = ?",
        (applied, _now(), refund_id),
    )


def ingest_refund(conn: sqlite3.Connection, payload: RefundPayload, delivery_id: str | None = None) -> dict:
    raw = payload.model_dump()
    with transaction(conn):
        existing = conn.execute("SELECT 1 FROM refunds WHERE id = ?", (payload.id,)).fetchone()
        if existing:
            _log_delivery(conn, "refunds", payload.id, delivery_id, "duplicate", raw)
            return {"status": "duplicate", "refund": _refund_view(_refund_row(conn, payload.id))}

        conn.execute(
            "INSERT INTO refunds (id, order_id, requested_cents, status, created_at, received_at)"
            " VALUES (?, ?, ?, 'pending', ?, ?)",
            (payload.id, payload.order_id, payload.amount_cents, payload.created_at, _now()),
        )
        order_exists = conn.execute("SELECT 1 FROM orders WHERE id = ?", (payload.order_id,)).fetchone()
        if order_exists:
            _apply_refund(conn, payload.id)
            _sync_ledger(conn, payload.order_id)
            outcome = "applied"
        else:
            outcome = "pending"
        _log_delivery(conn, "refunds", payload.id, delivery_id, outcome, raw)
        return {"status": outcome, "refund": _refund_view(_refund_row(conn, payload.id))}


# --- leitura ---------------------------------------------------------------

def _refund_row(conn, refund_id):
    return conn.execute("SELECT * FROM refunds WHERE id = ?", (refund_id,)).fetchone()


def _refund_view(r) -> dict:
    applied = r["applied_cents"]
    return {
        "id": r["id"],
        "order_id": r["order_id"],
        "status": r["status"],
        "requested": format_cents(r["requested_cents"]),
        "applied": format_cents(applied) if applied is not None else None,
        "ignored_excess": format_cents(r["requested_cents"] - applied) if applied is not None else None,
        "received_at": r["received_at"],
        "applied_at": r["applied_at"],
    }


def get_order(conn: sqlite3.Connection, order_id: str) -> dict | None:
    o = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not o:
        return None
    a = conn.execute("SELECT * FROM attributions WHERE order_id = ?", (order_id,)).fetchone()
    refunds = conn.execute("SELECT * FROM refunds WHERE order_id = ? ORDER BY seq", (order_id,)).fetchall()
    ledger = conn.execute(
        "SELECT * FROM attribution_ledger WHERE order_id = ? ORDER BY id", (order_id,)
    ).fetchall()

    refunded = sum(r["applied_cents"] or 0 for r in refunds if r["status"] == "applied")
    net = o["total_cents"] - refunded
    stage = order_status.stage(o["financial_status"], o["cancelled_at"])
    kinds = {l["kind"] for l in ledger}
    credited = sum(l["amount_cents"] for l in ledger)
    if a["creator_id"] is None:
        state = "unattributed"
    elif "cancel" in kinds or (stage == order_status.CANCELLED and "sale" not in kinds):
        state = "cancelled"
    elif "sale" not in kinds:
        state = "awaiting_payment"
    elif credited == o["total_cents"]:
        state = "active"
    elif credited == 0:
        state = "reverted"
    else:
        state = "partially_reverted"

    return {
        "id": o["id"],
        "total": format_cents(o["total_cents"]),
        "currency": o["currency"],
        "financial_status": o["financial_status"],
        "cancelled_at": o["cancelled_at"],
        "stage": stage,
        "coupon_codes": json.loads(o["coupon_codes"]),
        "utm": {k: o[k] for k in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")},
        "refunded": format_cents(refunded),
        "net": format_cents(net),
        "attribution": {
            "creator_id": a["creator_id"],
            "rule": a["rule"],
            "coupon_code": a["coupon_code"],
            "utm_campaign": a["utm_campaign"],
            "conflict": bool(a["conflict"]),
            "state": state,
            "credited": format_cents(credited),
        },
        "refunds": [_refund_view(r) for r in refunds],
        "ledger": [
            {
                "kind": l["kind"],
                "creator_id": l["creator_id"],
                "amount": format_cents(l["amount_cents"]),
                "refund_id": l["refund_id"],
                "created_at": l["created_at"],
            }
            for l in ledger
        ],
    }


def get_creator_summary(conn: sqlite3.Connection, creator_id: str) -> dict | None:
    c = conn.execute("SELECT * FROM creators WHERE id = ?", (creator_id,)).fetchone()
    if not c:
        return None
    row = conn.execute(
        "SELECT"
        " COALESCE(SUM(CASE WHEN kind = 'sale' THEN amount_cents END), 0) AS gross,"
        " COALESCE(-SUM(CASE WHEN kind = 'refund' THEN amount_cents END), 0) AS refunded,"
        " COALESCE(-SUM(CASE WHEN kind = 'cancel' THEN amount_cents END), 0) AS cancelled,"
        " COUNT(CASE WHEN kind = 'sale' THEN 1 END) AS orders"
        " FROM attribution_ledger WHERE creator_id = ?",
        (creator_id,),
    ).fetchone()
    return {
        "creator_id": c["id"],
        "name": c["name"],
        "orders": row["orders"],
        "gross": format_cents(row["gross"]),
        "refunded": format_cents(row["refunded"]),
        "cancelled": format_cents(row["cancelled"]),
        "net": format_cents(row["gross"] - row["refunded"] - row["cancelled"]),
    }
