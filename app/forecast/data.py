"""只读加载商品目录与连续日销量历史。"""

from collections.abc import Callable

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import get_read_session_factory
from app.errors import ForecastUnavailableError


CATALOG_SQL = text(
    """
    SELECT id AS product_id, sku, name, category, stock_quantity
    FROM products
    WHERE is_active = 1
    ORDER BY id
    """
)
SALES_SQL = text(
    """
    SELECT
        DATE(o.ordered_at) AS sales_date,
        p.id AS product_id,
        SUM(oi.quantity) AS quantity
    FROM order_items oi
    JOIN orders o ON o.id = oi.order_id
    JOIN products p ON p.id = oi.product_id
    JOIN payments pay
      ON pay.order_id = o.id
     AND pay.payment_status = 'success'
    WHERE o.order_status = 'paid' AND p.is_active = 1
    GROUP BY DATE(o.ordered_at), p.id
    ORDER BY sales_date, product_id
    """
)


def complete_daily_history(
    sales_rows: list[dict],
    catalog_rows: list[dict],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """把稀疏成交记录补齐为每商品每天一行，缺失销量按零处理。"""
    catalog = pd.DataFrame(catalog_rows)
    sales = pd.DataFrame(sales_rows)
    if catalog.empty or sales.empty:
        raise ForecastUnavailableError("forecast catalog or history is empty")
    sales["sales_date"] = pd.to_datetime(sales["sales_date"])
    sales["product_id"] = sales["product_id"].astype(int)
    sales["quantity"] = sales["quantity"].astype(float)
    dates = pd.date_range(sales["sales_date"].min(), sales["sales_date"].max(), freq="D")
    index = pd.MultiIndex.from_product(
        [catalog["product_id"].astype(int).tolist(), dates],
        names=["product_id", "sales_date"],
    )
    history = (
        sales.set_index(["product_id", "sales_date"])["quantity"]
        .reindex(index, fill_value=0.0)
        .rename("quantity")
        .reset_index()
    )
    catalog["product_id"] = catalog["product_id"].astype(int)
    catalog["stock_quantity"] = catalog["stock_quantity"].astype(int)
    return history, catalog


async def load_sales_data(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """使用只读账号加载模型推理和训练所需数据。"""
    resolved_factory = session_factory or get_read_session_factory()
    try:
        async with resolved_factory() as session:
            catalog_result = await session.execute(CATALOG_SQL)
            sales_result = await session.execute(SALES_SQL)
            catalog_rows = [dict(row) for row in catalog_result.mappings().all()]
            sales_rows = [dict(row) for row in sales_result.mappings().all()]
    except Exception as exc:
        raise ForecastUnavailableError("failed to load forecast data") from exc
    return complete_daily_history(sales_rows, catalog_rows)


SalesDataLoader = Callable[[], object]
