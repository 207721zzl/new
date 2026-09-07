# EvidenceRAG 当前完整链路图

> 更新于 2026-09-04。本文覆盖当前实际运行的 Web/API、文档索引、混合检索、问答、数据存储、Docker GPU 和临时公网链路。旧 Text2SQL、归因、预测和 MCP 不在默认运行时中，详见 `LEGACY.md`。

## 1. 全局端到端总览

```mermaid
flowchart LR
    User["用户"] --> Local["本机页面<br/>127.0.0.1:8000"]
    Remote["公网用户"] --> Edge["Cloudflare Edge"]
    Edge --> Tunnel["cloudflared Quick Tunnel"]
    Tunnel --> API
    Local --> API["FastAPI / EvidenceRAG"]

    Upload["PDF / DOCX / MD / HTML<br/>TXT / XLSX / XLS / JSON"] --> API
    API --> Parser["格式解析与结构块"]
    Parser --> Parent["结构感知父块"]
    Parent --> Child["重叠子块"]
    Parent --> MySQL["MySQL 真源"]
    Child --> BGE["BGE-M3 / CUDA"]
    BGE --> Milvus["Milvus Dense + BM25"]

    API --> Rewrite["多轮问题补全与改写"]
    Rewrite --> Retrieve["Dense + BM25 + RRF"]
    Milvus --> Retrieve
    Retrieve --> Reranker["bge-reranker-v2-m3"]
    Reranker --> Gate["证据门控"]
    Gate --> Restore["MySQL 父块恢复"]
    MySQL --> Restore
    Restore --> DeepSeek["DeepSeek 证据生成"]
    DeepSeek --> Result["答案 + 引用"]
```

## 2. Docker 启动链路

```mermaid
flowchart TB
    Start["docker compose up -d"] --> Infra["启动 mysql / etcd / minio"]
    Infra --> InfraHealth{"基础服务健康?"}
    InfraHealth -->|否| Wait1["等待或查看日志"]
    InfraHealth -->|是| Milvus["启动 Milvus standalone"]
    Milvus --> MilvusHealth{"Milvus 健康?"}
    MilvusHealth -->|是| API["api 执行 alembic upgrade head"]
    API --> Uvicorn["Uvicorn 监听容器 0.0.0.0:8000"]
    Uvicorn --> Models["只读加载 BGE-M3 与 Reranker 到 GPU"]
    Models --> Ready["/api/v1/health/ready = ready"]
    Ready --> Tunnel["启动 cloudflared"]
    Tunnel --> Public["生成随机 HTTPS 地址"]
```

宿主机端口只绑定到 `127.0.0.1`。公网访问通过 `tunnel` 容器到 Docker 网络中的 `api:8000`，不直接开放宿主机入站端口。

## 3. 上传与索引时序

```mermaid
sequenceDiagram
    participant B as 浏览器/客户端
    participant A as FastAPI
    participant J as MySQL Job Store
    participant P as Parser/Chunker
    participant E as BGE-M3
    participant V as Milvus

    B->>A: POST /api/v1/knowledge/uploads
    A->>A: 校验文件数、大小、扩展名并安全落盘
    A->>J: 创建 batch 与 pending jobs
    A-->>B: 202 + batch_id + status_url
    A->>A: BackgroundTasks 启动批次

    loop 同一批次按文件顺序
        A->>J: job = running
        A->>P: 解析为统一文档和结构块
        P->>P: 创建父块与重叠子块
        P->>J: 写入文档、父块和子块元数据
        loop 子块向量批次
            P->>E: embedding_text[]
            E-->>P: dense vectors[]
            P->>V: upsert chunks + vectors
        end
        A->>J: job = completed
    end

    B->>A: GET /api/v1/knowledge/uploads/{batch_id}
    A->>J: 汇总任务状态与数量
    A-->>B: pending/running/completed/partial_failed/failed
```

当前“异步”指请求无需等待整个索引完成，但实际任务仍运行在 API 进程内。同一批次顺序执行，适用于本机试点，不等同于持久化分布式任务队列。

## 4. 异步问答时序

```mermaid
sequenceDiagram
    participant B as 页面
    participant A as FastAPI
    participant M as MySQL
    participant R as RAG Workflow
    participant V as Milvus
    participant D as DeepSeek

    B->>A: POST /api/v1/runs
    A->>M: 创建 conversation 与 AgentRun
    A-->>B: 202 + run_id + events_url + result_url
    A->>R: BackgroundTasks 执行问答
    R->>M: 记录 workflow.started
    R->>D: 多轮问题补全/查询改写
    R->>V: Dense + BM25 检索
    R->>R: RRF + Reranker + 阈值

    alt 没有可靠证据
        R->>M: 保存拒答结果
    else 证据通过
        R->>M: 恢复 completed 父块
        R->>D: 基于证据生成答案
        R->>M: 保存答案、引用与 usage
    end

    par 本地或支持 SSE 的入口
        B->>A: GET /api/v1/runs/{run_id}/events
        A-->>B: SSE 节点事件
    and Quick Tunnel SSE 失败时
        B->>A: GET /api/v1/runs/{run_id}
        A-->>B: 轮询直到 completed/failed
    end
```

## 5. 混合检索详细链路

```mermaid
flowchart TB
    Q["原始问题"] --> Context["读取最近会话"]
    Context --> Standalone["独立问题"]
    Standalone --> Query["检索改写"]
    Query --> QVec["BGE-M3 查询向量"]
    QVec --> Dense["Milvus Dense Search"]
    Query --> Sparse["Milvus BM25 Search"]
    Dense --> RRF["Reciprocal Rank Fusion"]
    Sparse --> RRF
    RRF --> Candidates["扩大的候选集合"]
    Candidates --> Cross["Cross-Encoder Reranker"]
    Cross --> Threshold{"最高证据达到阈值?"}
    Threshold -->|否| Refusal["知识库资料不足"]
    Threshold -->|是| Dedup["按 parent_chunk_id 去重"]
    Dedup --> Parent["MySQL 恢复完整父块"]
    Parent --> Prompt["证据编号 + 来源元数据"]
    Prompt --> LLM["DeepSeek"]
    LLM --> Citation["回答 + 引用"]
```

检索分数和重排分数含义不同：RRF 分数用于融合排名，不是概率；最终证据顺序和门槛主要由 Reranker 决定。

## 6. 父子切块链路

```mermaid
flowchart LR
    Source["SourceBlock<br/>章节/页码/工作表"] --> Parent["父块<br/>目标约 1600 字符"]
    Parent --> C1["子块 1<br/>约 400 字符"]
    Parent --> C2["子块 2<br/>重叠约 80 字符"]
    Parent --> C3["子块 N"]
    C1 --> Index["Milvus 检索"]
    C2 --> Index
    C3 --> Index
    Index --> ID["parent_chunk_id"]
    ID --> Restore["MySQL 完整父块"]
```

父块优先遵循标题、PDF 页面、表格和 Excel 工作表边界；子块不跨父块。检索使用小块提高召回，生成使用父块保持上下文完整。

## 7. 数据与状态关系

```mermaid
flowchart TB
    UploadDir["data/knowledge/uploads<br/>上传原文件"] --> API
    API --> Admin["MySQL 管理连接"]
    Admin --> Docs["documents / parent_chunks / child metadata"]
    Admin --> Jobs["indexing jobs / batches"]
    Admin --> Runs["conversations / runs / events / feedback"]

    API --> Vector["Milvus collection"]
    Vector --> Dense["Dense vector"]
    Vector --> BM25["BM25 sparse function"]
    Vector --> Meta["chunk_id / parent_chunk_id / source metadata"]

    Milvus["Milvus"] --> Etcd["etcd metadata"]
    Milvus --> MinIO["MinIO object data"]
```

MySQL 是可审计真源，Milvus 是可重建索引。只有 MySQL 与向量写入完成的文档才进入在线父块恢复。

## 8. 公网链路

```mermaid
sequenceDiagram
    participant U as 公网用户
    participant C as Cloudflare
    participant T as cloudflared 容器
    participant A as API 容器

    T->>C: 主动建立出站加密连接
    C-->>T: 分配随机 trycloudflare.com
    U->>C: HTTPS 请求
    C->>T: 经隧道转发
    T->>A: HTTP api:8000
    A-->>U: 页面或 API 响应
```

当前临时公网入口不带账号密码。它用于演示，不是固定域名方案；重启后地址变化，异常时容器可能仍为 `Up`，但应以公网 HTTP 验证和 tunnel 日志为准。

## 9. 失败与恢复边界

| 场景 | 当前行为 |
|---|---|
| 文件类型或大小不合法 | 创建后台任务前拒绝 |
| 单个索引任务失败 | MySQL 标记 `failed`，批次可为 `partial_failed` |
| API 在入库中停止 | 进程内 BackgroundTask 中断，需要重新提交或补偿 |
| Milvus 有命中但父块缺失 | 显式检索故障，不静默生成 |
| 证据低于阈值 | 返回知识不足，不调用最终生成 |
| SSE 中断 | 页面改为轮询运行结果 |
| Tunnel 断线 | 本地服务继续可用；公网需重连并获取新地址 |
| `docker compose stop` | 停止服务但保留镜像、模型和持久数据 |

## 10. 当前代码锚点

| 能力 | 主要位置 |
|---|---|
| FastAPI 页面、健康、问答与上传 API | `app/main.py` |
| 上传批次与索引执行 | `app/operations/service.py` |
| 文档解析器 | `app/ingestion/` |
| 父子切块 | `app/rag/documents.py` |
| BGE-M3 | `app/rag/embeddings.py` |
| Milvus 混合检索 | `app/rag/milvus_store.py` |
| Reranker 与问答服务 | `app/rag/reranker.py`、`app/rag/service.py` |
| MySQL 模型与仓储 | `app/db/` |
| 页面 SSE 与轮询回退 | `app/static/app.js` |
| Docker GPU、模型挂载与隧道 | `Dockerfile`、`docker-compose.yml` |

## 11. 当前边界与下一阶段

- 当前入库批次顺序执行，适合少量试点文档；
- OCR 仅在配置开启且 PDF 缺少文本时使用；
- Quick Tunnel 无认证、非固定地址且无生产可用性保证；
- 没有租户隔离、文档级权限过滤或独立任务队列；
- 大批量阶段应增加 Celery/Redis Worker、增量指纹、断点续跑、GPU 批处理、Milvus Bulk Import 和蓝绿 Collection。

## 12. 一句话总结

`多格式文档 → 结构解析 → 父子切块 → BGE-M3 → Milvus Dense/BM25 → RRF → bge-reranker-v2-m3 → 证据门控 → MySQL 父块 → DeepSeek → 带引用回答`。
