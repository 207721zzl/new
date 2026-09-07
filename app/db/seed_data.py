"""用于本地演示和测试的确定性电商模拟数据。"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class SeedBundle:
    """按外键写入顺序组织的模拟数据集合。"""

    customers: list[dict]
    campaigns: list[dict]
    products: list[dict]
    orders: list[dict]
    order_items: list[dict]
    payments: list[dict]
    refunds: list[dict]
    daily_traffic: list[dict]
    metric_definitions: list[dict]


def current_week_start(today: date | None = None) -> date:
    """返回给定日期所在周的周一。"""
    resolved = today or date.today()
    return resolved - timedelta(days=resolved.weekday())


def build_seed_bundle(anchor_date: date, history_days: int = 180) -> SeedBundle:
    """生成锚点前指定天数的确定性历史数据。

    默认 180 天满足销量预测的滞后特征和时间序列回测。华东区最近一周的
    件数被确定性下调，便于继续演示 GMV 环比下降及原因分析。
    相同锚点始终生成相同主键和金额，因此脚本可以安全重复 Upsert。
    """
    product_rows = [
        {
            "id": 1,
            "sku": "PHONE-001",
            "name": "旗舰手机",
            "category": "手机",
            "unit_price": Decimal("3999.00"),
            "stock_quantity": 80,
            "is_active": True,
        },
        {
            "id": 2,
            "sku": "EAR-001",
            "name": "降噪耳机",
            "category": "耳机",
            "unit_price": Decimal("699.00"),
            "stock_quantity": 120,
            "is_active": True,
        },
        {
            "id": 3,
            "sku": "PAD-001",
            "name": "学习平板",
            "category": "平板",
            "unit_price": Decimal("2499.00"),
            "stock_quantity": 65,
            "is_active": True,
        },
        {
            "id": 4,
            "sku": "WATCH-001",
            "name": "智能手表",
            "category": "穿戴",
            "unit_price": Decimal("1299.00"),
            "stock_quantity": 90,
            "is_active": True,
        },
        {
            "id": 5,
            "sku": "KEY-001",
            "name": "机械键盘",
            "category": "外设",
            "unit_price": Decimal("499.00"),
            "stock_quantity": 140,
            "is_active": True,
        },
        {
            "id": 6,
            "sku": "MOUSE-001",
            "name": "无线鼠标",
            "category": "外设",
            "unit_price": Decimal("199.00"),
            "stock_quantity": 160,
            "is_active": True,
        },
    ]
    prices = {row["id"]: row["unit_price"] for row in product_rows}
    regions = ("华东", "华南", "华北")
    channels = ("自然搜索", "付费投放", "站内推荐")
    period_start = anchor_date - timedelta(days=history_days)
    customer_rows = [
        {
            "id": region_index * 30 + customer_index + 1,
            "customer_no": f"CUST-{region_index + 1}-{customer_index + 1:03d}",
            "region": region,
            "customer_level": ("普通", "银卡", "金卡")[customer_index % 3],
            "registered_at": datetime.combine(
                period_start - timedelta(days=customer_index + 30),
                time(hour=9),
            ),
        }
        for region_index, region in enumerate(regions)
        for customer_index in range(30)
    ]
    campaign_rows = [
        {
            "id": 1,
            "campaign_code": "CAMPAIGN-FOUNDATION",
            "name": "基础拉新活动",
            "channel": "自然搜索",
            "start_at": datetime.combine(period_start, time.min),
            "end_at": datetime.combine(anchor_date - timedelta(days=90), time.min),
            "budget": Decimal("30000.00"),
            "status": "completed",
        },
        {
            "id": 2,
            "campaign_code": "CAMPAIGN-GROWTH",
            "name": "年中增长活动",
            "channel": "付费投放",
            "start_at": datetime.combine(anchor_date - timedelta(days=90), time.min),
            "end_at": datetime.combine(anchor_date - timedelta(days=30), time.min),
            "budget": Decimal("60000.00"),
            "status": "completed",
        },
        {
            "id": 3,
            "campaign_code": "CAMPAIGN-RECENT",
            "name": "近期转化活动",
            "channel": "站内推荐",
            "start_at": datetime.combine(anchor_date - timedelta(days=30), time.min),
            "end_at": datetime.combine(anchor_date, time.min),
            "budget": Decimal("45000.00"),
            "status": "completed",
        },
    ]

    orders: list[dict] = []
    order_items: list[dict] = []
    payments: list[dict] = []
    refunds: list[dict] = []
    daily_traffic: list[dict] = []
    if history_days < 90:
        raise ValueError("history_days must be at least 90")

    for day_offset in range(history_days):
        business_date = period_start + timedelta(days=day_offset)
        days_before_anchor = (anchor_date - business_date).days
        is_latest_week = days_before_anchor <= 7
        for region_index, region in enumerate(regions):
            # 日期和区域共同组成稳定主键；锚点向后滚动时，重叠日期仍会命中
            # 同一记录，新日期则自然追加，避免顺序 ID 与唯一订单号发生冲突。
            order_id = int(business_date.strftime("%Y%m%d")) * 100 + region_index + 1
            product_id = (
                (business_date.toordinal() + region_index) % len(product_rows)
            ) + 1

            if region == "华东":
                quantity = (
                    (2 + business_date.day % 2)
                    if is_latest_week
                    else (4 + business_date.day % 2)
                )
            else:
                quantity = 2 + ((business_date.day + region_index) % 2)

            unit_price = prices[product_id]
            line_amount = (unit_price * quantity).quantize(Decimal("0.01"))
            ordered_at = datetime.combine(business_date, time(hour=12 + region_index))
            customer_id = region_index * 30 + business_date.toordinal() % 30 + 1
            if days_before_anchor <= 30:
                campaign_id = 3
            elif days_before_anchor <= 90:
                campaign_id = 2
            else:
                campaign_id = 1

            orders.append(
                {
                    "id": order_id,
                    "order_no": f"SEED-{business_date:%Y%m%d}-{region_index + 1}",
                    "customer_id": customer_id,
                    "campaign_id": campaign_id,
                    "region": region,
                    "order_status": "paid",
                    "order_amount": line_amount,
                    "ordered_at": ordered_at,
                }
            )
            order_items.append(
                {
                    "id": order_id,
                    "order_id": order_id,
                    "product_id": product_id,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "line_amount": line_amount,
                }
            )
            payments.append(
                {
                    "id": order_id,
                    "order_id": order_id,
                    "paid_amount": line_amount,
                    "payment_status": "success",
                    "paid_at": ordered_at + timedelta(minutes=5),
                }
            )

            if (business_date.toordinal() + region_index) % 6 == 0:
                refunds.append(
                    {
                        "id": order_id,
                        "refund_no": f"REF-{business_date:%Y%m%d}-{region_index + 1}",
                        "order_id": order_id,
                        "order_item_id": order_id,
                        "refund_amount": (line_amount * Decimal("0.20")).quantize(
                            Decimal("0.01")
                        ),
                        "refund_status": "success",
                        "refunded_at": ordered_at + timedelta(days=1),
                    }
                )

            visits = 80 + (business_date.toordinal() + region_index * 7) % 41
            add_to_cart_users = 12 + (business_date.day + region_index) % 8
            checkout_users = max(3, add_to_cart_users - 5)
            paid_users = 1 + (business_date.day + region_index) % 3
            daily_traffic.append(
                {
                    "id": order_id,
                    "traffic_date": business_date,
                    "region": region,
                    "channel": channels[region_index],
                    "campaign_id": campaign_id,
                    "visits": visits,
                    "product_views": visits * 3 + business_date.day,
                    "add_to_cart_users": add_to_cart_users,
                    "checkout_users": checkout_users,
                    "paid_users": min(paid_users, checkout_users),
                }
            )

    metric_definitions = [
        {
            "id": 1,
            "metric_code": "gmv",
            "metric_name": "支付 GMV",
            "description": "统计周期内支付成功订单的实付金额合计。",
            "formula": "SUM(payments.paid_amount) WHERE payment_status = 'success'",
            "unit": "元",
            "time_grain": "day",
            "source_tables": ["orders", "payments"],
            "version": "1.0",
            "is_active": True,
        },
        {
            "id": 2,
            "metric_code": "paid_order_count",
            "metric_name": "支付订单量",
            "description": "统计周期内支付成功的去重订单数量。",
            "formula": "COUNT(DISTINCT payments.order_id) WHERE payment_status = 'success'",
            "unit": "单",
            "time_grain": "day",
            "source_tables": ["payments"],
            "version": "1.0",
            "is_active": True,
        },
        {
            "id": 3,
            "metric_code": "refund_amount_rate",
            "metric_name": "退款金额率",
            "description": (
                "统计周期内成功退款金额占同期支付成功商品金额的百分比；"
                "退款按 refunded_at 归属，支付按 paid_at 归属。"
            ),
            "formula": (
                "100 * SUM(refunds.refund_amount WHERE refund_status = 'success' "
                "AND refunded_at IN period) / NULLIF(SUM(payments.paid_amount "
                "WHERE payment_status = 'success' AND paid_at IN period), 0)"
            ),
            "unit": "%",
            "time_grain": "day",
            "source_tables": ["payments", "refunds"],
            "version": "1.1",
            "is_active": True,
        },
        {
            "id": 4,
            "metric_code": "traffic_visits",
            "metric_name": "访问用户数",
            "description": "统计周期内各区域和渠道访问用户数合计。",
            "formula": "SUM(daily_traffic.visits)",
            "unit": "人次",
            "time_grain": "day",
            "source_tables": ["daily_traffic"],
            "version": "1.0",
            "is_active": True,
        },
        {
            "id": 5,
            "metric_code": "payment_conversion_rate",
            "metric_name": "支付转化率",
            "description": "支付用户数占访问用户数的百分比。",
            "formula": "100 * SUM(daily_traffic.paid_users) / NULLIF(SUM(daily_traffic.visits), 0)",
            "unit": "%",
            "time_grain": "day",
            "source_tables": ["daily_traffic"],
            "version": "1.0",
            "is_active": True,
        },
    ]

    return SeedBundle(
        customers=customer_rows,
        campaigns=campaign_rows,
        products=product_rows,
        orders=orders,
        order_items=order_items,
        payments=payments,
        refunds=refunds,
        daily_traffic=daily_traffic,
        metric_definitions=metric_definitions,
    )
