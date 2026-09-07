"""Create the initial commerce data foundation.

Revision ID: 20260716_0001
Revises:
Create Date: 2026-07-16
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260716_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create product, order, payment, refund and metric tables."""
    op.create_table(
        "products",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "unit_price >= 0", name=op.f("ck_products_unit_price_non_negative")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_products"),
        sa.UniqueConstraint("sku", name="uq_products_sku"),
    )
    op.create_index("ix_products_category", "products", ["category"], unique=False)

    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_no", sa.String(length=64), nullable=False),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("order_status", sa.String(length=32), nullable=False),
        sa.Column("order_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("ordered_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "order_amount >= 0",
            name=op.f("ck_orders_order_amount_non_negative"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_orders"),
        sa.UniqueConstraint("order_no", name="uq_orders_order_no"),
    )
    op.create_index(
        "ix_orders_ordered_at_region",
        "orders",
        ["ordered_at", "region"],
        unique=False,
    )

    op.create_table(
        "order_items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("line_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.CheckConstraint(
            "quantity > 0", name=op.f("ck_order_items_quantity_positive")
        ),
        sa.CheckConstraint(
            "unit_price >= 0",
            name=op.f("ck_order_items_unit_price_non_negative"),
        ),
        sa.CheckConstraint(
            "line_amount >= 0",
            name=op.f("ck_order_items_line_amount_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_items_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_order_items_product_id_products",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_items"),
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"])
    op.create_index("ix_order_items_product_id", "order_items", ["product_id"])

    op.create_table(
        "payments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("paid_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("payment_status", sa.String(length=32), nullable=False),
        sa.Column("paid_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "paid_amount >= 0",
            name=op.f("ck_payments_paid_amount_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_payments_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payments"),
        sa.UniqueConstraint("order_id", name="uq_payments_order_id"),
    )
    op.create_index("ix_payments_paid_at", "payments", ["paid_at"])

    op.create_table(
        "refunds",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("refund_no", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("order_item_id", sa.BigInteger(), nullable=True),
        sa.Column("refund_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("refund_status", sa.String(length=32), nullable=False),
        sa.Column("refunded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "refund_amount >= 0",
            name=op.f("ck_refunds_refund_amount_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_refunds_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_item_id"],
            ["order_items.id"],
            name="fk_refunds_order_item_id_order_items",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_refunds"),
        sa.UniqueConstraint("refund_no", name="uq_refunds_refund_no"),
    )
    op.create_index("ix_refunds_order_id", "refunds", ["order_id"])
    op.create_index("ix_refunds_refunded_at", "refunds", ["refunded_at"])

    op.create_table(
        "metric_definitions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("metric_code", sa.String(length=64), nullable=False),
        sa.Column("metric_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("formula", sa.Text(), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("time_grain", sa.String(length=32), nullable=False),
        sa.Column("source_tables", sa.JSON(), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_metric_definitions"),
        sa.UniqueConstraint("metric_code", name="uq_metric_definitions_metric_code"),
    )


def downgrade() -> None:
    """Drop all tables in reverse foreign-key order."""
    op.drop_table("metric_definitions")
    op.drop_table("refunds")
    op.drop_table("payments")
    op.drop_table("order_items")
    op.drop_table("orders")
    op.drop_table("products")
