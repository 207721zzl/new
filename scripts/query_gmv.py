"""通过数据库只读账号查询指定周期和区域的支付 GMV。"""

import argparse
import asyncio
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json

from app.db.repositories import BusinessMetricRepository
from app.db.seed_data import current_week_start
from app.db.session import dispose_database_engines, get_read_session_factory


def parse_args() -> argparse.Namespace:
    """解析左闭右开的日期范围和可选区域。"""
    week_start = current_week_start()
    parser = argparse.ArgumentParser(description="Query trusted GMV from MySQL.")
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=week_start - timedelta(days=7),
    )
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=week_start,
    )
    parser.add_argument("--region", default="华东")
    return parser.parse_args()


async def query_gmv(
    start_date: date,
    end_date: date,
    region: str | None,
) -> Decimal:
    """使用只读会话执行可信 GMV 查询。"""
    start_at = datetime.combine(start_date, time.min)
    end_at = datetime.combine(end_date, time.min)
    async with get_read_session_factory()() as session:
        return await BusinessMetricRepository(session).gmv(start_at, end_at, region)


async def run_query(
    start_date: date,
    end_date: date,
    region: str | None,
) -> Decimal:
    """在同一事件循环中执行查询并释放数据库连接池。"""
    try:
        return await query_gmv(start_date, end_date, region)
    finally:
        await dispose_database_engines()


def main() -> int:
    """执行查询并输出 JSON 结果。"""
    args = parse_args()
    value = asyncio.run(run_query(args.start_date, args.end_date, args.region))
    print(
        json.dumps(
            {
                "metric": "gmv",
                "region": args.region,
                "start_date": args.start_date.isoformat(),
                "end_date": args.end_date.isoformat(),
                "value": str(value),
                "unit": "元",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
