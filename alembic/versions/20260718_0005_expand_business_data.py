"""Expand business data for customer, campaign and traffic analysis.

Revision ID: 20260718_0005
Revises: 20260718_0004
Create Date: 2026-07-18
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260718_0005"
down_revision: str | Sequence[str] | None = "20260718_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create dimensions and traffic funnel, then link orders."""
    op.create_table(
        "customers",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("customer_no", sa.String(64), nullable=False),
        sa.Column("region", sa.String(32), nullable=False),
        sa.Column("customer_level", sa.String(32), nullable=False),
        sa.Column("registered_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customers")),
        sa.UniqueConstraint("customer_no", name=op.f("uq_customers_customer_no")),
    )
    op.create_index(op.f("ix_customers_region"), "customers", ["region"])

    op.create_table(
        "campaigns",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("campaign_code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("start_at", sa.DateTime(), nullable=False),
        sa.Column("end_at", sa.DateTime(), nullable=False),
        sa.Column("budget", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("budget >= 0", name=op.f("ck_campaigns_budget_non_negative")),
        sa.CheckConstraint("end_at > start_at", name=op.f("ck_campaigns_campaign_period_valid")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaigns")),
        sa.UniqueConstraint("campaign_code", name=op.f("uq_campaigns_campaign_code")),
    )
    op.create_index(op.f("ix_campaigns_channel"), "campaigns", ["channel"])

    op.add_column("orders", sa.Column("customer_id", sa.BigInteger(), nullable=True))
    op.add_column("orders", sa.Column("campaign_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(op.f("fk_orders_customer_id_customers"), "orders", "customers", ["customer_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key(op.f("fk_orders_campaign_id_campaigns"), "orders", "campaigns", ["campaign_id"], ["id"], ondelete="SET NULL")
    op.create_index(op.f("ix_orders_customer_id"), "orders", ["customer_id"])
    op.create_index(op.f("ix_orders_campaign_id"), "orders", ["campaign_id"])

    op.create_table(
        "daily_traffic",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("traffic_date", sa.Date(), nullable=False),
        sa.Column("region", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(64), nullable=False),
        sa.Column("campaign_id", sa.BigInteger(), nullable=True),
        sa.Column("visits", sa.Integer(), nullable=False),
        sa.Column("product_views", sa.Integer(), nullable=False),
        sa.Column("add_to_cart_users", sa.Integer(), nullable=False),
        sa.Column("checkout_users", sa.Integer(), nullable=False),
        sa.Column("paid_users", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("visits >= 0", name=op.f("ck_daily_traffic_visits_non_negative")),
        sa.CheckConstraint("product_views >= 0", name=op.f("ck_daily_traffic_product_views_non_negative")),
        sa.CheckConstraint("add_to_cart_users >= 0", name=op.f("ck_daily_traffic_cart_users_non_negative")),
        sa.CheckConstraint("checkout_users >= 0", name=op.f("ck_daily_traffic_checkout_users_non_negative")),
        sa.CheckConstraint("paid_users >= 0", name=op.f("ck_daily_traffic_paid_users_non_negative")),
        sa.CheckConstraint("product_views >= visits", name=op.f("ck_daily_traffic_views_cover_visits")),
        sa.CheckConstraint("visits >= add_to_cart_users", name=op.f("ck_daily_traffic_visits_cover_cart_users")),
        sa.CheckConstraint("add_to_cart_users >= checkout_users", name=op.f("ck_daily_traffic_cart_users_cover_checkout_users")),
        sa.CheckConstraint("checkout_users >= paid_users", name=op.f("ck_daily_traffic_checkout_users_cover_paid_users")),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], name=op.f("fk_daily_traffic_campaign_id_campaigns"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_daily_traffic")),
    )
    op.create_index(op.f("ix_daily_traffic_traffic_date"), "daily_traffic", ["traffic_date"])
    op.create_index(op.f("ix_daily_traffic_region"), "daily_traffic", ["region"])
    op.create_index(op.f("ix_daily_traffic_channel"), "daily_traffic", ["channel"])
    op.create_index(op.f("ix_daily_traffic_campaign_id"), "daily_traffic", ["campaign_id"])


def downgrade() -> None:
    """Remove traffic and order dimension links."""
    op.drop_table("daily_traffic")
    op.drop_index(op.f("ix_orders_campaign_id"), table_name="orders")
    op.drop_index(op.f("ix_orders_customer_id"), table_name="orders")
    op.drop_constraint(op.f("fk_orders_campaign_id_campaigns"), "orders", type_="foreignkey")
    op.drop_constraint(op.f("fk_orders_customer_id_customers"), "orders", type_="foreignkey")
    op.drop_column("orders", "campaign_id")
    op.drop_column("orders", "customer_id")
    op.drop_table("campaigns")
    op.drop_table("customers")
