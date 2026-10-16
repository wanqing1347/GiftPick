# GiftPick · 礼物推荐智能体

`giftpick-agent` 是一个**送礼推荐 Agent**，它基于 [AgentScope 2.0](https://github.com/modelscope/agentscope)
的多 Agent 能力，采用 DDD 洋葱架构，后端 FastAPI + AG-UI 协议，前端 React + Vite，并内置了一套可复现的 Harness 评测流程。

---

## 一、项目定位与边界

**只做选购与推荐**：商品检索、品类洞察、跨境到手价估算、送礼偏好管理

---

## 二、核心能力

| 能力 | 说明 |
| --- | --- |
| 多轮选购对话 | 每个 `shopping_session_id` 绑定一个 MainAgent 实例，`AgentState` 落盘，服务重启后恢复多轮上下文 |
| 多 Agent 协作 | MainAgent 总管，按需通过 `task_dispatch` 并发调度 ProductAgent / ResearchAgent，子 Agent 独立上下文隔离 |
| 二阶段检索 | embedding 向量召回 + 专用 HTTP/DashScope 精排；可选 BM25 混合召回 |
| 品类洞察 RAG | 品类热卖款型、属性判断口径、价格区间、避坑与合规知识库 |
| 跨境到手价 | 商品卡内联 `landed_price`（小计 + 运费 + 关税估算），支持目的国可达性过滤 |
| 送礼与长期记忆 | 买家偏好 LLM 提炼 + 语义召回；收礼人标签记忆；`dislike` 作为硬约束全量保留 |
| 上下文治理 | 分层上下文、压力触发裁剪、完整摘要、稳定前缀候选、Token 预算与降级 |
| 运行时护栏 | 单步断言、循环检测、L3 内容过滤、L4 输出审核、工具审批 |
| 可靠性 | 工具熔断、网关配额闸门、模型回退与有界重试、Redis Stream 队列削峰、会话 fence 与 owner 绑定 |
| 可观测 | OTLP / Langfuse Trace、进程内运维指标、Prometheus 暴露 |
| Harness 评测 | 契约门禁 + 真实模型冒烟 + 收益门禁，证据可校验、可跨版本对照 |

---

## 三、系统架构

### 3.1 分层（DDD 洋葱）

```
presentation  →  application  →  domain  ←  infrastructure
 (FastAPI/AG-UI)   (agents/usecases)  (领域模型/端口)   (适配器实现)
```

依赖方向由外向内：`domain` 只定义实体与端口，`infrastructure` 实现端口，`application` 编排用例，
`presentation` 负责传输。所有接线集中在 [`app/composition.py`](app/composition.py)（Composition Root），
API 进程与 worker 进程共用同一份装配，避免行为漂移。

### 3.2 Agent 体系

```
                    ┌─────────────────────────────┐
   买家消息  ─────▶ │  MainAgent (CommerceConcierge)│
                    │  业务工具 / Task 计划四件套     │
                    │  task_dispatch / 记忆工具       │
                    └──────────────┬──────────────┘
                       并发派发（上下文隔离）
                ┌──────────────┴───────────────┐
                ▼                               ▼
      ┌──────────────────┐            ┌──────────────────┐
      │  ProductAgent     │            │  ResearchAgent    │
      │  商品检索专家      │            │  联网调研专家      │
      │  product_search   │            │  web_search(可选)  │
      │  category_insight │            │  fetch_webpage    │
      │  conversation_... │            └──────────────────┘
      └──────────────────┘
```

- **MainAgent**：`app/application/agents/main_agent.py`。工具分四类——业务工具、内置 Task 计划工具、
  调度工具 `task_dispatch`、长期记忆写工具。
- **ProductAgent**：`app/application/agents/product_agent.py`。持有检索与品类洞察工具，主 Agent 也可直接复用同一批工具单干。
- **ResearchAgent**：`app/application/agents/research_agent.py`。联网搜索（需 `TAVILY_API_KEY`）+ 网页正文抓取，
  长资料不进入主对话上下文。
- **编排器**：`app/application/agents/orchestrator.py`。注入会话快照与偏好 hint，把 AgentScope 类型化事件流
  映射为 `TradeEventBus` 事件，负责上下文压缩检测与上游瞬时错误重试。

### 3.3 运行时请求链路

```
前端(React/AG-UI)
  │  POST /commerce/ag-ui/run (SSE)   或   POST /commerce/intents
  ▼
FastAPI (presentation)
  │  可选：入 Redis Stream 队列
  ▼
MainAgentOrchestrator ── MainAgent ──┬── ProductAgent  ── 向量检索/Qdrant ── Reranker
  │                                  └── ResearchAgent ── Tavily / 网页抓取
  │  事件 → TradeEventBus → WebSocket / SSE
  ▼
SQLite / JSON 文件持久化（会话、偏好、收礼人记忆、表单、Skill…）
```

---

## 四、目录结构

```
app/
├── domain/          领域层：catalog（商品/SKU/价格/检索端口）、buyer（偏好/收礼人）、session、queue、shipping
├── application/
│   ├── agents/      MainAgent / ProductAgent / ResearchAgent / orchestrator / context_service
│   ├── tools/       product_search、category_insight、task_dispatch、记忆与表单等工具
│   ├── memory/      偏好选择器与记忆中间件
│   ├── harness/     单步断言、循环检测
│   ├── prompts/     giftpick.yml 提示词集合与加载器
│   └── usecases/    检索、商品媒体等用例
├── infrastructure/  适配器：LLM/embedding/rerank、Qdrant、RAG、缓存、队列、持久化、安全、追踪、指标
├── presentation/    FastAPI 路由、AG-UI、会话连接、买家工作区、上下文/收藏/表单接口
├── composition.py   装配容器（Composition Root）
└── worker.py        队列消费进程入口
frontend/            React + Vite + AG-UI 客户端（商品卡、购物计划、上下文/买家工作区）
```

> 本仓库只公开运行时源码（`app/`、`frontend/`）与构建配置。以下内部资产**不随仓库分发**：
> `docs/`（改动记录）、`tests/`（测试）、`scripts/`（评测/生成/冒烟脚本）、
> `knowledge/`（品类知识库）、`data/`（商品目录）、`eval/`（Harness 套件与原始证据）。
> 因此下方涉及这些路径的命令均需在具备完整内部资产的私有工作区中执行。

---

## 五、快速开始

### 5.1 环境要求

- Python `3.11 ~ 3.13`（项目锁定 3.11，AgentScope 2.x 要求 ≥ 3.11）
- [uv](https://docs.astral.sh/uv/) 包管理器
- Node.js 22+（仅前端需要）
- 可选：Redis、Qdrant、Reranker 服务、Tavily Key、OTLP/Langfuse

### 5.2 配置

复制 `.env.example` 为 `.env`（`.env` 已被 gitignore，勿提交真实密钥），至少配置 LLM 网关：

```bash
cp .env.example .env
# 也可直接用环境变量（优先级高于 .env）
export LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export LLM_API_KEY=sk-xxx
export LLM_MODEL=qwen3-max
```

关键配置分组（完整列表见 [`.env.example`](.env.example)）：

| 分组 | 变量 |
| --- | --- |
| 模型 | `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` / `LLM_FALLBACK_MODEL` |
| 向量与精排 | `EMBEDDING_MODEL`、`QDRANT_URL`、`RERANKER_MODE` / `RERANKER_MODEL` / `RERANKER_BASE_URL` |
| 存储 | `DATA_DIR`、`DATABASE_URL`（缺省 SQLite，`file` 退回 JSON 文件） |
| 缓存与队列 | `REDIS_URL`、`SEMANTIC_CACHE_ENABLED`、`QUEUE_ENABLED` |
| 上下文 | `CONTEXT_STRATEGY`、`CONTEXT_PRUNING_TIMING`、`CONTEXT_TARGET_TOKENS`、`TOKEN_BUDGET_TOTAL` |
| 安全与身份 | `IDENTITY_MODE`、`SESSION_OWNER_BINDING`、`HARNESS_ENABLED`、`OUTPUT_GUARD_ENABLED` |
| 记忆 | `PREFERENCE_TOP_K`、`PREFERENCE_SUBAGENT_INJECT` |
| 可观测 | `OTEL_EXPORTER_OTLP_*`、`LANGFUSE_*` |

### 5.3 本地运行

```bash
uv sync                                  # 安装依赖（含 dev 组）

# 启动 API（默认端口 8000）
uv run uvicorn app.presentation.server:app --port 8000

# 如启用队列（配了 REDIS_URL 且 QUEUE_ENABLED=1），另起 worker 消费进程
uv run python -m app.worker

# 健康检查：依赖连通性、队列深度、prompt registry 状态
curl http://localhost:8000/health
```

### 5.4 前端

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
# 可通过 VITE_API_BASE / VITE_BUYER_ID / VITE_API_TOKEN 指定后端地址与身份
```

### 5.5 Docker Compose（全栈）

```bash
export LLM_BASE_URL=<网关地址> LLM_API_KEY=<密钥>
docker compose -f docker/docker-compose.yaml up -d --build
# 前端 http://localhost:5173，后端 http://localhost:8000
```

Compose 会拉起 `app` + `worker` + `redis` + `qdrant` + `frontend`；存储用 SQLite（`app-data` 卷），
向量库指向 `qdrant` 服务。

---

## 六、API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/commerce/intents` | 提交买家意图，同步返回最终回复（启用队列时内部入队等待） |
| `POST` | `/commerce/intents/async` | 提交意图，立即返回 `task_id` |
| `GET` | `/commerce/tasks/{task_id}` | 查询任务状态（queued/running/done/failed） |
| `WS` | `/commerce/events` | 订阅会话事件流（工具调用、计划、最终结果） |
| `POST` | `/commerce/ag-ui/run` | AG-UI 标准运行入口（POST + SSE），前端主通道 |
| `GET` | `/commerce/ag-ui/runs/{run_id}` | 读取历史 run 与事件 |
| `POST` | `/commerce/ag-ui/runs/{run_id}/cancel` | 取消运行 |
| `GET` | `/commerce/skills`、`/commerce/my-skills` | 只读 Skill 目录与买家 Skill 管理 |
| `GET/POST/DELETE` | `/commerce/preferences` | 长期偏好的读取、写入与删除（含历史） |
| `GET/PUT/DELETE` | `/commerce/favorites` | 收藏商品 |
| `GET/POST` | `/commerce/shopping-forms` | A2UI v0.9 选购表单交互 |
| `GET/POST` | `/commerce/context`、`/commerce/context/compact` | 上下文查看与手动整理 |
| `GET` | `/health` | 健康检查（数据库/Redis/队列/prompt registry） |
| `GET` | `/internal/metrics`、`/internal/metrics/summary` | 运维指标（需签名主体授权） |

---

## 七、关键设计说明

### 7.1 上下文治理
分层策略（`CONTEXT_STRATEGY=layered`）区分完整近期轮次与可裁剪的历史，压力触发裁剪（`pressure`），
商品结果按 `CONTEXT_PRODUCT_TOKENS` 单独限额，超限走完整摘要。`stable_prefix` + `preserve_prefix`
等候选策略默认关闭，需通过 Harness 收益门禁后才启用。

### 7.2 长期记忆与送礼
- 买家偏好由 LLM 提炼后写入 `DATA_DIR/buyer_memory.db`，按当前选购范围做语义召回；
  `dislike` 作为硬约束**不参与截断**，避免把忌口/过敏项裁掉。
- 收礼人记忆按**精确标签**查询，不猜别名；送礼上下文不会用买家自身偏好做硬过滤。
- 写记忆需买家确认（工具审批），保存失败不回退原文。

### 7.3 检索与到手价
embedding（OpenAI 兼容 `/v1/embeddings`，默认 `text-embedding-v4`，1024 维）+ Qdrant 向量召回，
再经专用 reranker 精排；`HYBRID_RECALL_ENABLED` 可开启 BM25 混合召回（默认关闭，收益未验收）。
商品卡在传入 `ship_to` 时内联 `landed_price`（小计 + 运费 + 关税）。向量库首次启动自动建库灌数，
embedding 模型/版本变化时用 `EMBEDDING_VERSION` 区分。

### 7.4 可靠性与安全
- **工具级**：连续失败达阈值熔断（可跨实例共享）；结果按 `TOOL_RESULT_LIMIT` 截断。
- **网关级**：全进程唯一配额闸门限制并发与请求间隔；瞬时故障有界重试 + 模型回退，并显式发 `model.fallback` 事件。
- **请求级**：可选 `TOKEN_BUDGET_TOTAL` 调用前预留、按实际 usage 结算，预算不足时不再调模型。
- **护栏**：循环检测、单步断言、L3 内容过滤、L4 输出审核；默认开启零成本护栏，会额外调模型的候选一律默认关闭。
- **身份**：`IDENTITY_MODE=demo` 为演示身份；生产需 `hmac` 并配置 `IDENTITY_HMAC_SECRET`，会话 owner 绑定与
  fence 防止跨会话写入。

### 7.5 队列与削峰
配置 Redis 后，意图进入 Redis Stream 队列，由 `app/worker.py` 消费；长会话走低优先级队列，
worker 通过执行租约保证只处理有效任务，未 ack 消息由 pending 机制重投。

---

## 八、测试与评测（内部工作区）

> 本章列出的测试与评测资产（`tests/`、`scripts/`、`eval/`、`knowledge/`、`data/`）**不随本仓库分发**，
> 须在具备完整内部资产的私有工作区中执行；公开仓库中这些命令不可直接运行。

### 8.1 单元/契约测试

```bash
uv run pytest                 # 全量测试（tests/，97 个测试文件）
uv run pytest tests/test_retrieval.py -q
```

### 8.2 Harness 评测（修改 Harness 相关代码时必跑）

Harness 覆盖确定性安全契约与真实模型冒烟，档位包括 `contracts` / `smoke` / `dev` / `release`：

```bash
make eval-harness            # 安全契约 + 完整真实模型冒烟 + 同源码证据检查
make eval-harness-contracts  # 仅确定性安全契约
make eval-harness-dev        # 开发集
make eval-harness-release    # 发布门禁（需证明真实收益）

# 影响范围检查 / 跨版本对照 / 证据一致性校验
uv run python -m scripts.eval.harness impact --files <路径...>
uv run python -m scripts.eval.harness compare --baseline <旧目录> --candidate <新目录> --output <比较目录>
uv run python -m scripts.eval.harness verify <运行目录>
```

冻结套件位于 `eval/harness/v1/`（48 个场景：40 上下文 + 5 Agent 工作流 + 2 Skill 回放 + 1 缓存回放），
每次运行写入新目录并保留 `report.html`、请求账本、失败样本与源码指纹，原始证据放 `eval/verification/`。
**评测程序的运行状态与门禁结论以内部改动记录为准，不以本文档或单测通过替代。**

### 8.3 端到端冒烟

```bash
uv run uvicorn app.presentation.server:app --port 8000
uv run python scripts/smoke_e2e.py --query "预算300元以内，找一个轻便的周末旅行背包，寄到中国。"
```



