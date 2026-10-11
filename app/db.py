import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS creators (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

-- Cada cupom e cada utm_campaign pertence a no máximo um criador.
CREATE TABLE IF NOT EXISTS creator_coupons (
    code       TEXT PRIMARY KEY,           -- normalizado em maiúsculas
    creator_id TEXT NOT NULL REFERENCES creators(id)
);

CREATE TABLE IF NOT EXISTS creator_utm_campaigns (
    campaign   TEXT PRIMARY KEY,           -- normalizado em minúsculas
    creator_id TEXT NOT NULL REFERENCES creators(id)
);

CREATE TABLE IF NOT EXISTS orders (
    id               TEXT PRIMARY KEY,     -- id do pedido na loja: chave de idempotência
    total_cents      INTEGER NOT NULL CHECK (total_cents >= 0),
    currency         TEXT NOT NULL,
    financial_status TEXT NOT NULL,        -- último status aceito (só avança, ver app/order_status.py)
    cancelled_at     TEXT,
    coupon_codes     TEXT NOT NULL,        -- JSON
    utm_source       TEXT,
    utm_medium       TEXT,
    utm_campaign     TEXT,
    utm_content      TEXT,
    utm_term         TEXT,
    created_at       TEXT,
    received_at      TEXT NOT NULL
);

-- Uma linha por pedido: a decisão de atribuição é tomada uma vez e não é apagada.
CREATE TABLE IF NOT EXISTS attributions (
    order_id     TEXT PRIMARY KEY REFERENCES orders(id),
    creator_id   TEXT REFERENCES creators(id),   -- NULL = venda sem criador
    rule         TEXT NOT NULL,
    coupon_code  TEXT,
    utm_campaign TEXT,
    conflict     INTEGER NOT NULL DEFAULT 0,     -- cupom e UTM apontavam para criadores diferentes
    decided_at   TEXT NOT NULL
);

-- Sem FK para orders: o estorno pode chegar antes do pedido.
CREATE TABLE IF NOT EXISTS refunds (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,  -- ordem de chegada
    id              TEXT NOT NULL UNIQUE,               -- id do estorno: chave de idempotência
    order_id        TEXT NOT NULL,
    requested_cents INTEGER NOT NULL CHECK (requested_cents >= 0),
    applied_cents   INTEGER CHECK (applied_cents >= 0), -- NULL enquanto pendente
    status          TEXT NOT NULL CHECK (status IN ('pending', 'applied')),
    created_at      TEXT,
    received_at     TEXT NOT NULL,
    applied_at      TEXT
);
CREATE INDEX IF NOT EXISTS refunds_order_idx ON refunds(order_id, status);

-- Livro-razão só de inserção: venda (+), estornos (-) e cancelamento (-) por criador.
CREATE TABLE IF NOT EXISTS attribution_ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT NOT NULL REFERENCES orders(id),
    creator_id   TEXT NOT NULL REFERENCES creators(id),
    kind         TEXT NOT NULL CHECK (kind IN ('sale', 'refund', 'cancel')),
    amount_cents INTEGER NOT NULL,
    refund_id    TEXT UNIQUE REFERENCES refunds(id),
    created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ledger_one_sale_per_order
    ON attribution_ledger(order_id) WHERE kind = 'sale';
CREATE UNIQUE INDEX IF NOT EXISTS ledger_one_cancel_per_order
    ON attribution_ledger(order_id) WHERE kind = 'cancel';

-- Toda entrega de webhook, inclusive duplicadas, fica registrada.
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic       TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    delivery_id TEXT,
    outcome     TEXT NOT NULL,
    payload     TEXT NOT NULL,
    received_at TEXT NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    # BEGIN IMMEDIATE serializa escritores: dois webhooks iguais simultâneos
    # não conseguem ambos passar pela checagem de "já existe".
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
