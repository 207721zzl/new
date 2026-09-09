## 问题与结果

<!-- 说明触发问题的场景，以及改动后用户或服务看到的行为。 -->

## 主要改动

- <!-- 请填写主要改动。 -->

## 验证

<!-- 列出实际运行的测试或检查，以及需要特定 GPU、模型或外部服务的未覆盖部分。 -->

- [ ] `python -m pytest -q`
- [ ] `python -m ruff check app services tests_rag tests_microservices`
- [ ] Compose 配置检查通过

## 提交前检查

- [ ] 未提交 `.env`、API Key、真实业务文档、模型或数据库卷
- [ ] 已更新受影响的文档和测试
- [ ] 已说明兼容性影响和迁移步骤（如适用）
