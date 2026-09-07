# 原项目回滚参考（不属于当前运行系统）

当前目录没有 Git 仓库。为避免改造过程中不可逆删除原项目实现，以下旧代码暂时保留，但已从 EvidenceRAG 的入口、依赖、容器、页面和默认测试中隔离：

- `app/analysis/`：经营分析与 Text2SQL。
- `app/forecast/`：销量预测。
- `app/intent/`、`app/graph.py`：旧意图路由和 LangGraph。
- `app/mcp/`：旧 MCP 工具服务。
- 原 `tests/` 和非 RAG 脚本：旧项目回归参考。
- `docs/FORECAST_MODEL.md`：旧销量预测模型契约，仅用于回滚和历史设计参考。

EvidenceRAG 的有效入口是 `app/main.py`，核心测试位于 `tests_rag/`。确认不再需要回滚后，可在建立版本控制和备份后删除上述文件；它们不是当前应用运行所需代码。

当前架构与运行链路以 `README.md`、`docs/ARCHITECTURE.md` 和 `docs/PROJECT_CHAIN_DIAGRAM.md` 为准。旧模块仍保留源码不代表在线入口会加载或路由到这些能力。
