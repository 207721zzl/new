"""Text2SQL 可见的业务 Schema 与表、字段白名单。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ColumnSchema:
    """单个允许查询字段的类型与业务含义。"""

    name: str
    data_type: str
    description: str


@dataclass(frozen=True, slots=True)
class TableSchema:
    """单个允许查询表及其显式字段集合。"""

    name: str
    description: str
    columns: tuple[ColumnSchema, ...]


BUSINESS_SCHEMA: tuple[TableSchema, ...] = (
    TableSchema(
        "customers",
        "匿名化客户维表；用于区域与客户等级分析",
        (
            ColumnSchema("id", "BIGINT", "客户主键"),
            ColumnSchema("customer_no", "VARCHAR", "匿名客户编号"),
            ColumnSchema("region", "VARCHAR", "客户所属区域"),
            ColumnSchema("customer_level", "VARCHAR", "客户等级"),
            ColumnSchema("registered_at", "DATETIME", "注册时间"),
            ColumnSchema("created_at", "DATETIME", "创建时间"),
        ),
    ),
    TableSchema(
        "campaigns",
        "营销活动维表；包含渠道、预算和生效周期",
        (
            ColumnSchema("id", "BIGINT", "活动主键"),
            ColumnSchema("campaign_code", "VARCHAR", "稳定活动编码"),
            ColumnSchema("name", "VARCHAR", "活动名称"),
            ColumnSchema("channel", "VARCHAR", "投放渠道"),
            ColumnSchema("start_at", "DATETIME", "活动开始时间"),
            ColumnSchema("end_at", "DATETIME", "活动结束时间"),
            ColumnSchema("budget", "DECIMAL", "活动预算"),
            ColumnSchema("status", "VARCHAR", "活动状态"),
            ColumnSchema("created_at", "DATETIME", "创建时间"),
        ),
    ),
    TableSchema(
        "products",
        "商品维表",
        (
            ColumnSchema("id", "BIGINT", "商品主键"),
            ColumnSchema("sku", "VARCHAR", "商品 SKU"),
            ColumnSchema("name", "VARCHAR", "商品名称"),
            ColumnSchema("category", "VARCHAR", "标准品类"),
            ColumnSchema("unit_price", "DECIMAL", "商品标准单价"),
            ColumnSchema("stock_quantity", "INT", "当前可用库存数量"),
            ColumnSchema("is_active", "BOOLEAN", "是否在售"),
            ColumnSchema("created_at", "DATETIME", "创建时间"),
        ),
    ),
    TableSchema(
        "orders",
        "订单主表；区域维度在本表",
        (
            ColumnSchema("id", "BIGINT", "订单主键"),
            ColumnSchema("order_no", "VARCHAR", "订单号"),
            ColumnSchema("customer_id", "BIGINT", "关联 customers.id"),
            ColumnSchema("campaign_id", "BIGINT", "关联 campaigns.id"),
            ColumnSchema("region", "VARCHAR", "销售区域，如华东、华南、华北"),
            ColumnSchema("order_status", "VARCHAR", "订单状态"),
            ColumnSchema("order_amount", "DECIMAL", "订单金额"),
            ColumnSchema("ordered_at", "DATETIME", "下单时间"),
            ColumnSchema("created_at", "DATETIME", "创建时间"),
        ),
    ),
    TableSchema(
        "order_items",
        "订单商品明细事实表",
        (
            ColumnSchema("id", "BIGINT", "明细主键"),
            ColumnSchema("order_id", "BIGINT", "关联 orders.id"),
            ColumnSchema("product_id", "BIGINT", "关联 products.id"),
            ColumnSchema("quantity", "INT", "购买数量"),
            ColumnSchema("unit_price", "DECIMAL", "成交单价"),
            ColumnSchema("line_amount", "DECIMAL", "明细金额"),
        ),
    ),
    TableSchema(
        "payments",
        "支付事实表；GMV 以成功支付金额为准",
        (
            ColumnSchema("id", "BIGINT", "支付主键"),
            ColumnSchema("order_id", "BIGINT", "关联 orders.id"),
            ColumnSchema("paid_amount", "DECIMAL", "实付金额"),
            ColumnSchema("payment_status", "VARCHAR", "支付状态，成功值为 success"),
            ColumnSchema("paid_at", "DATETIME", "支付时间"),
        ),
    ),
    TableSchema(
        "refunds",
        "退款事实表",
        (
            ColumnSchema("id", "BIGINT", "退款主键"),
            ColumnSchema("refund_no", "VARCHAR", "退款单号"),
            ColumnSchema("order_id", "BIGINT", "关联 orders.id"),
            ColumnSchema("order_item_id", "BIGINT", "关联 order_items.id"),
            ColumnSchema("refund_amount", "DECIMAL", "退款金额"),
            ColumnSchema("refund_status", "VARCHAR", "退款状态，成功值为 success"),
            ColumnSchema("refunded_at", "DATETIME", "退款时间"),
        ),
    ),
    TableSchema(
        "daily_traffic",
        "按日期、区域、渠道汇总的流量与支付转化漏斗",
        (
            ColumnSchema("id", "BIGINT", "流量记录主键"),
            ColumnSchema("traffic_date", "DATE", "业务日期"),
            ColumnSchema("region", "VARCHAR", "区域"),
            ColumnSchema("channel", "VARCHAR", "流量渠道"),
            ColumnSchema("campaign_id", "BIGINT", "关联 campaigns.id"),
            ColumnSchema("visits", "INT", "访问用户数"),
            ColumnSchema("product_views", "INT", "商品浏览次数"),
            ColumnSchema("add_to_cart_users", "INT", "加购用户数"),
            ColumnSchema("checkout_users", "INT", "提交结算用户数"),
            ColumnSchema("paid_users", "INT", "支付用户数"),
            ColumnSchema("created_at", "DATETIME", "创建时间"),
        ),
    ),
    TableSchema(
        "metric_definitions",
        "指标语义定义；通常由应用注入 Prompt，不需要在指标计算 SQL 中读取",
        (
            ColumnSchema("id", "BIGINT", "定义主键"),
            ColumnSchema("metric_code", "VARCHAR", "稳定指标编码"),
            ColumnSchema("metric_name", "VARCHAR", "指标名称"),
            ColumnSchema("description", "TEXT", "指标描述"),
            ColumnSchema("formula", "TEXT", "指标公式"),
            ColumnSchema("unit", "VARCHAR", "指标单位"),
            ColumnSchema("time_grain", "VARCHAR", "默认时间粒度"),
            ColumnSchema("source_tables", "JSON", "指标来源表"),
            ColumnSchema("version", "VARCHAR", "口径版本"),
            ColumnSchema("is_active", "BOOLEAN", "是否启用"),
            ColumnSchema("updated_at", "DATETIME", "更新时间"),
        ),
    ),
)


ALLOWED_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    table.name: frozenset(column.name for column in table.columns)
    for table in BUSINESS_SCHEMA
}

SYSTEM_DATABASES = frozenset({"information_schema", "mysql", "performance_schema", "sys"})


def render_schema_description() -> str:
    """把唯一白名单定义渲染为供 Text2SQL 使用的紧凑 Schema。"""
    lines: list[str] = []
    for table in BUSINESS_SCHEMA:
        lines.append(f"表 {table.name}：{table.description}")
        for column in table.columns:
            lines.append(
                f"- {table.name}.{column.name} {column.data_type}："
                f"{column.description}"
            )
    return "\n".join(lines)
