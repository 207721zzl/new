"""Add product inventory used by replenishment recommendations.

Revision ID: 20260718_0003
Revises: 20260718_0002
Create Date: 2026-07-18
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260718_0003"
down_revision: str | Sequence[str] | None = "20260718_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a non-negative current stock snapshot to products."""
    op.add_column(
        "products",
        sa.Column(
            "stock_quantity",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_products_stock_quantity_non_negative"),
        "products",
        "stock_quantity >= 0",
    )


def downgrade() -> None:
    """Remove the inventory snapshot."""
    op.drop_constraint(
        op.f("ck_products_stock_quantity_non_negative"),
        "products",
        type_="check",
    )
    op.drop_column("products", "stock_quantity")
