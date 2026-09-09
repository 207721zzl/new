# 参与贡献

感谢你参与 EvidenceRAG。代码、文档、测试、问题复现和产品建议都欢迎提交。

## 开始之前

1. 先搜索现有 Issue，确认问题没有被重复报告。
2. 较大的功能或架构调整请先创建 Issue，说明使用场景、预期行为和影响范围。
3. 安全漏洞不要发布为公开 Issue，请按 [SECURITY.md](SECURITY.md) 报告。

## 开发流程

1. Fork 仓库并从 `main` 创建功能分支。
2. 按 README 完成本地配置，不要把 `.env`、模型、真实文档或运行数据提交到仓库。
3. 保持改动聚焦，必要时同步更新测试与文档。
4. 提交 Pull Request，说明问题、改动后的行为和验证结果。

推荐的分支名称：

```text
feat/short-description
fix/short-description
docs/short-description
```

## 验证

在提交 Pull Request 前运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app services tests_rag tests_microservices
docker compose --env-file .env --env-file .env.microservices -f deploy/compose.yml config --quiet
```

如果改动依赖 GPU、外部模型或第三方 API，请在 Pull Request 中注明实际测试环境和未覆盖的部分。

## 代码与提交

- Python 代码遵循现有类型标注、异步调用和错误处理方式。
- 服务之间通过明确的 HTTP 或队列契约交互，避免跨服务直接访问业务表。
- 用户可见文字应清楚、简洁，并兼顾桌面端和移动端。
- 提交信息建议使用 `feat:`、`fix:`、`docs:`、`refactor:`、`test:` 等前缀。

提交贡献即表示你同意按项目的 MIT 许可证发布该贡献。
