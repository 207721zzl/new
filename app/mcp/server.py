"""InsightAgent 独立 FastMCP Streamable HTTP 服务入口。"""

from fastmcp import FastMCP

from app.config import get_settings
from app.mcp.models import (
    ReadOnlySQLRequest,
    ReadOnlySQLResponse,
    SalesForecastToolRequest,
    SalesForecastToolResponse,
    TableSchemaRequest,
    TableSchemaResponse,
)
from app.mcp.tools import get_tool_service


mcp = FastMCP("InsightAgent MCP")


@mcp.tool(
    name="get_table_schema",
    description="查询经营分析只读 SQL 可访问的表、字段、类型和业务含义。",
)
async def get_table_schema(request: TableSchemaRequest) -> TableSchemaResponse:
    """返回全部或指定白名单表结构。"""
    return await get_tool_service().get_table_schema(request)


@mcp.tool(
    name="execute_readonly_sql",
    description=(
        "在 SQLGlot AST 白名单、最大行数、EXPLAIN、超时和 MySQL 只读账号"
        "约束下执行一条 SELECT。"
    ),
)
async def execute_readonly_sql(
    request: ReadOnlySQLRequest,
) -> ReadOnlySQLResponse:
    """安全执行单条经营分析 SELECT。"""
    return await get_tool_service().execute_readonly_sql(request)


@mcp.tool(
    name="forecast_sales",
    description=(
        "依据连续历史日销量和动态日期范围加载本地全局 PyTorch 模型预测，"
        "由 DeepSeek 解释，并由后端确定性计算补货建议。"
    ),
)
async def forecast_sales(
    request: SalesForecastToolRequest,
) -> SalesForecastToolResponse:
    """执行销量预测与补货建议。"""
    return await get_tool_service().forecast_sales(request)


def main() -> None:
    """仅绑定配置地址启动内部 Streamable HTTP 服务。"""
    settings = get_settings()
    mcp.run(
        transport="http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        path=settings.mcp_path,
    )


if __name__ == "__main__":
    main()
