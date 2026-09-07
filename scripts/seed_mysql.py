"""向 MySQL 幂等写入本地演示用电商数据和指标定义。"""

import argparse
import asyncio
from datetime import date
import json

from sqlalchemy.dialects.mysql import insert

from app.db.models import (
    Campaign,
    Customer,
    DailyTraffic,
    MetricDefinition,
    Order,
    OrderItem,
    Payment,
    Product,
    Refund,
)
from app.config import get_settings
from app.db.seed_data import build_seed_bundle, current_week_start
from app.db.session import dispose_database_engines, get_admin_engine
from app.logging_config import get_logger


logger = get_logger("scripts.seed_mysql")


def parse_args() -> argparse.Namespace:
    """解析模拟数据时间锚点；锚点应为当前周周一。"""
    parser = argparse.ArgumentParser(
        description="Seed deterministic commerce data into MySQL.",
    )
    parser.add_argument(
        "--anchor-date",
        type=date.fromisoformat,
        help="Current week Monday in YYYY-MM-DD; defaults to this week's Monday.",
    )
    parser.add_argument(
        "--history-days",
        type=int,
        default=get_settings().forecast_history_days,
        help="Number of deterministic history days to seed (minimum 90).",
    )
    return parser.parse_args()


async def upsert_rows(connection, model, rows: list[dict]) -> int:
    """按主键或唯一键 Upsert 一组数据并返回输入行数。"""
    if not rows:
        return 0
    statement = insert(model).values(rows)
    provided_columns = set(rows[0])
    updates = {
        column.name: statement.inserted[column.name]
        for column in model.__table__.columns
        if not column.primary_key and column.name in provided_columns
    }
    await connection.execute(statement.on_duplicate_key_update(**updates))
    return len(rows)


async def seed(anchor_date: date, history_days: int) -> dict[str, int | str]:
    """在一个事务中按外键顺序写入全部模拟数据。"""
    bundle = build_seed_bundle(anchor_date, history_days=history_days)
    engine = get_admin_engine()
    async with engine.begin() as connection:
        counts = {
            "customers": await upsert_rows(connection, Customer, bundle.customers),
            "campaigns": await upsert_rows(connection, Campaign, bundle.campaigns),
            "products": await upsert_rows(connection, Product, bundle.products),
            "orders": await upsert_rows(connection, Order, bundle.orders),
            "order_items": await upsert_rows(connection, OrderItem, bundle.order_items),
            "payments": await upsert_rows(connection, Payment, bundle.payments),
            "refunds": await upsert_rows(connection, Refund, bundle.refunds),
            "daily_traffic": await upsert_rows(
                connection,
                DailyTraffic,
                bundle.daily_traffic,
            ),
            "metric_definitions": await upsert_rows(
                connection,
                MetricDefinition,
                bundle.metric_definitions,
            ),
        }
    logger.info("mysql seed completed anchor_date=%s counts=%s", anchor_date, counts)
    return {
        "anchor_date": anchor_date.isoformat(),
        "history_days": history_days,
        **counts,
    }


async def run_seed(anchor_date: date, history_days: int) -> dict[str, int | str]:
    """在同一事件循环中执行灌数并释放数据库连接池。"""
    try:
        return await seed(anchor_date, history_days)
    finally:
        await dispose_database_engines()


def main() -> int:
    """执行模拟数据 Upsert 并输出机器可读摘要。"""
    args = parse_args()
    anchor_date = args.anchor_date or current_week_start()
    summary = asyncio.run(run_seed(anchor_date, args.history_days))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
