"""FastMCP 真实进程内协议注册与结构化响应冒烟测试。"""

import asyncio

from fastmcp import Client

from app.mcp.server import mcp


def test_fastmcp_registers_three_tools_and_exposes_pydantic_output():
    async def scenario():
        async with Client(mcp) as client:
            tools = await client.list_tools()
            result = await client.call_tool(
                "get_table_schema",
                {"request": {"table_name": "orders"}},
            )
            return tools, result

    tools, result = asyncio.run(scenario())

    assert {tool.name for tool in tools} == {
        "get_table_schema",
        "execute_readonly_sql",
        "forecast_sales",
    }
    assert result.structured_content is not None
    assert result.structured_content["tables"][0]["name"] == "orders"
