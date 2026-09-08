# 微服务运行与迁移手册

## 实现范围

新系统入口为 `services/gateway/api.py`。身份、问答、知识库、推理分别使用 `services/identity`、`services/chat`、`services/knowledge`、`services/inference`。

`app/main.py` 和原 `docker-compose.yml` 保留为原系统回退入口。新系统使用 `deploy/compose.yml`，拥有单独的 Compose 项目和持久卷，不挂载原数据库卷；默认访问端口为 18000。原代码不是新服务的入口，新服务不引用 `app.db` 或其他服务的 ORM。

Gateway 继续提供员工问答、登录/注册和管理员静态页面。三类页面共用蓝白浅色视觉语言；员工端的检索过程和引用位于可展开证据面板中，回答反馈仍提交到 `/api/v1/feedback`。

核心行为：

- API 创建任务和 `durable_tasks` 记录在同一事务内完成；派发器通过 Redis/Celery 分队列投递，数据库保留可重新派发的任务真源。
- 执行器定期更新租约，数据库写入检查执行代号并加锁。失去执行权的执行器无法发布结果，已完成任务再次投递不会再次生成助手消息。
- RAG 工作流、个人会话、消息和反馈属于问答库；账号和 Cookie 会话属于身份库。入口与业务服务通过身份服务校验会话，保留 CSRF 和角色权限。
- 模型服务常驻加载 Embedding 和 Reranker，用单消费者、有界优先队列保护 GPU；在线请求先于尚未开始的入库请求执行。
- 知识快照存储原文及父子块 JSON，`document_heads` 保存可见版本指针。向量写入成功后才发布快照，整批重建在所有文件成功后一起切换集合。检索过滤非当前版本，并在重排后重新检查可见性。
- 文档逻辑删除与本地审计和清理任务同事务提交。异步删除精确的旧 chunk ID，不按 document_id 删除新版本；审计通过幂等接口汇聚至身份服务。
- 新服务的管理看板聚合领域统计。统计不可用时显示“暂不可用”；跨域维护返回各领域状态并支持同一个 Idempotency-Key 重试。

## 本地启动

保留现有 `.env` 中的模型目录和 DeepSeek 配置，初始化独立密码：

```powershell
.venv/Scripts/python.exe -m scripts.init_microservices
docker compose --env-file .env --env-file .env.microservices -f deploy/compose.yml config --quiet
./scripts/start_microservices.ps1 -Build
```

初始化脚本只在配置不存在时生成随机密码，不输出密码、不覆盖现有配置。`.env.microservices` 已加入忽略规则。先在本机访问 `http://127.0.0.1:18000`；新旧系统拥有不同的数据，在迁移完成前不要切换正式访问入口。

局域网试运行并要求项目持久资源位于 D 盘时，使用：

```powershell
./scripts/start_lan_microservices.ps1 -Build
```

该入口会把当前进程的临时目录切换到项目下的 `volumes/microservices-runtime/tmp`，并在启动前检查项目、模型和 Docker Desktop 的 WSL 数据盘都位于 D 盘。Gateway 绑定 `0.0.0.0:${MICRO_API_PORT:-18000}`，数据库、Redis、MinIO、Milvus 仍只在 Compose 内部网络可见。Windows 防火墙应仅允许 TCP 18000 的 `LocalSubnet` 入站访问；不要把 MySQL、Redis、MinIO 或 Milvus 端口开放到局域网。

临时公网 HTTPS 试运行使用 Cloudflare Quick Tunnel：

```powershell
./scripts/start_public_microservices.ps1 -Build
Get-Content ./volumes/public-tunnel/public-url.txt
```

公网入口在独立的 `deploy/compose.public.yml` 中启用，只把 Gateway 连接到隧道；MySQL、Redis、MinIO、Milvus 和模型服务没有公网端口。隧道与 Gateway 共享容器网络空间，使 Uvicorn 只接受来自本机代理的原始 HTTPS 协议，并为公网登录签发 `Secure` Cookie。隧道镜像和运行状态随 Docker Desktop 数据盘保存在 D 盘，当前公网地址写入 `volumes/public-tunnel/public-url.txt`。

Quick Tunnel 不需要域名或 Cloudflare 账号，适合当前验收，但它没有可用性保证，容器重新创建后地址会变化。固定域名和长期运行应改用预先创建的命名 Tunnel，并把凭据作为本机机密提供；不要提交到仓库。停止公网入口时执行：

```powershell
docker compose --profile public --env-file .env --env-file .env.microservices `
  -f deploy/compose.yml -f deploy/compose.public.yml stop public-tunnel
```

Compose 中包含三个一次性 Alembic 迁移任务、对象桶初始化任务、两个 Worker 和两个派发器。迁移先于领域 API 启动，API 不在启动时执行数据库迁移或把所有活动任务标记失败。

新装环境创建首个管理员：

```powershell
docker compose --env-file .env --env-file .env.microservices -f deploy/compose.yml exec identity python -m services.identity.create_admin --username admin --display-name 系统管理员
```

迁移已有用户时不需要先创建新管理员，已有账号和密码哈希随数据复制。管理员和员工入口保持互斥。

## 数据迁移

迁移工具默认只读检查。执行前停止旧 API 的新写入并排空活动问答与入库任务；在旧实例留下数据库和上传文件的一致性备份。不要在无法确认备份可恢复时切换正式访问入口。

迁移容器通过 `SOURCE_DATABASE_URL` 读取源库。源库权限只需 SELECT；地址应是容器可访问的主机地址，例如宿主机数据库使用 `host.docker.internal`。通过当前进程环境提供连接串，不把密码写入命令参数、文档或日志。

```powershell
docker compose --env-file .env --env-file .env.microservices -f deploy/compose.yml -f deploy/migrate.yml run --rm import-legacy
# 确认源 API 已停止写入且在途任务为零后，执行复制。
docker compose --env-file .env --env-file .env.microservices -f deploy/compose.yml -f deploy/migrate.yml run --rm import-legacy python -m scripts.migrate_microservices --execute --source-quiesced
```

迁移工具：

1. 用只读一致性快照检查源表和活动任务。
2. 复制身份、会话、消息、运行、反馈、历史入库任务；保留 ID、密码哈希和登录会话令牌摘要。
3. 核对各表数量和内容指纹。目标中已有不同内容时拒绝覆盖，已完整复制且内容相同的表允许再次检查。
4. 用源库的已完成文档和结构父块生成上传对象，创建新系统入库任务。新索引由 Worker 重建，不修改原 Milvus 集合。
5. 待所有迁移入库任务完成，再验证登录、历史会话、文档数量及固定问题的引用。迁移完成状态不等于索引已经完成。

文档导入发生部分失败时，目标可能已有待处理的迁移批次，工具会保护已有数据而拒绝覆盖。先核对已经提交的批次，必要时在另一个空目标环境重新迁移；源数据库始终不被迁移脚本修改。工具目前按表读取数据，超大库应改成主键分页批次后再迁移。

## 验证与回退

```powershell
.venv/Scripts/python.exe -m pytest
.venv/Scripts/python.exe -m ruff check services packages tests_microservices scripts/init_microservices.py scripts/migrate_microservices.py scripts/reconcile_knowledge_storage.py
docker compose --env-file .env --env-file .env.microservices -f deploy/compose.yml ps
```

自动测试使用三个隔离的 SQLite 数据库验证所有权、外键、事务逻辑、任务幂等、失效租约、接口契约、代理 Cookie/SSE、身份与问答链路，以及文档发布/重建/删除竞争。模型、对象存储和向量后端在这些测试中使用替身；这不能替代 MySQL 行锁、真实 Redis/Celery、Milvus 和 GPU 容器联调。

正式切换前还需进行：终止 Worker 后恢复任务、重启 Redis 后重新派发、多副本并发领取任务、GPU 混合上传与查询压测、真实资料引用质量对照、备份恢复与回退演练。MySQL、Milvus、Redis 和 GPU 的生产容量阈值需用真实负载确定。

新版本首次试运行期间使用 18000 端口，原系统继续保留。新系统没有新增写入时，可将入口切回原系统；已经接收新用户、会话或文档后，不能直接切旧库，必须先停止新写入、备份并同步新增数据。不要使用 `down -v`，以免删除新系统持久卷。

## 维护

任务可观察字段包括 `status`、`attempts`、`generation`、`owner`、`lease_until`、`published_at` 和 `error`。达到重试上限会进入失败终态，不会无限热循环。需要人工重跑时，应先检查失败原因，避免对未知外部副作用反复执行。

清理超过 24 小时的不可达知识快照和未被引用的上传对象，先预览再执行：

```powershell
docker compose --env-file .env --env-file .env.microservices -f deploy/compose.yml exec knowledge python -m scripts.reconcile_knowledge_storage
docker compose --env-file .env --env-file .env.microservices -f deploy/compose.yml exec knowledge python -m scripts.reconcile_knowledge_storage --execute
```

清理工具保留当前版本和仍在执行的任务引用。逻辑删除后的文件会在引用解除且超过保留缓冲期后回收。切换后遗留的空 Milvus 集合暂保留，避免自动删除仍需回退的集合。

当前提供统一请求 ID、run/job 日志关联、Redis 汇总入口指标和领域统计；尚未接入完整 OpenTelemetry 跨服务跨度。Compose 的 MySQL 账号仅获得本领域数据库的权限，但迁移与运行当前使用同一领域账号，后续可进一步分离 DDL 与 DML 账号。

## 当前环境状态

2026-09-08 已在本机完成独立微服务 Compose 部署。Gateway、身份、问答、知识库、GPU 推理、两个 Worker、两个派发器、MySQL、Redis、MinIO 和 Milvus 均已启动；BGE-M3 与 Reranker 从 D 盘只读挂载并在 CUDA 上加载。示例文档完成真实对象存储、解析、向量化和索引，员工账号完成检索、异步问答和引用生成，管理员端收到真实反馈和 Token 统计。

局域网入口与临时公网 HTTPS 入口均已验证。旧系统数据仍保留在原存储中，尚未执行历史数据迁移、容量压测、恢复演练或固定域名切换，因此当前状态属于可访问的单机验收部署，不代表多机高可用生产环境。
