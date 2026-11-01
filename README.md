# DataPilot

中文问数（NL2SQL）。输入一个问题，系统读表结构、写 SQL、在只读库上执行，报错或结果可疑时带着反馈重写，最后返回答案、SQL、表格和图表，支持追问。

这个项目的重点不在功能多，而在每个选型都有对照实验：用固定流程还是 Agent、要不要 schema 检索、用哪个模型、要不要路由，都在同一批题上做了配对检验。所有数字和运行 ID 在 [docs/numbers.md](docs/numbers.md)。

![演示](docs/images/demo.gif)

## 主要结论

| 问题 | 结论 | 依据 |
|---|---|---|
| 固定流程还是 Agent | 固定流程。准确率一样，Agent 成本高 56%、延迟高 55% | BIRD 500 题，Sonnet 72.0% vs 72.0%，p=1.0 |
| 哪些改进有用 | 取值核对加执行纠错，Haiku 63.0% → 68.0%（p=0.008）；对 Sonnet 不显著 | 逐项消融，3 次运行 |
| 要不要 schema 检索 | schema 放得下就不要。准确率显著下降（p=0.013），成本 2.9 倍 | 373 题配对 |
| 用哪个模型 | Sonnet 72.4% 最高；qwen3.7-plus 68.0% 与 Haiku 67.4% 持平（p=0.81），单题 $0.0004 | 500 题 |
| 要不要路由 | Haiku 先答，触发纠错再升级 Sonnet：69.6%，成本是全用 Sonnet 的 14% | 离线回放 3 组，实跑一致 |
| 中文提问会不会掉分 | 不会，中英文没有显著差别 | BIRD 同批 100 题配对 |
| 企业问数难在哪 | 口径和元数据。去掉口径说明后，各模型从 93–98% 掉到 55–66%，换更强的模型也补不回来 | 模拟电商库 40 题 |
| 怎么防止查到敏感数据 | 行列级权限由代码强制，越权 0/170 | 46 个绕过测试，3 个角色 |
| 标准答案靠得住吗 | 失败题里 56% 是标准答案本身有问题（审计论文报告 52.8%） | 50 道人工判定 |

![问数界面](docs/images/chat.png)

![评测看板](docs/images/eval.png)

## 架构

```
浏览器（Next.js + ECharts）
  │  SSE 推送：步骤 → SQL → 表格 → 图表 → 回答，15 秒心跳
  ▼
FastAPI ── Redis：会话（按用户隔离，保留最近 3 轮）、查询缓存、限流计数
  │         Redis 连不上时退回内存
  │  限流 429 → 缓存命中直接返回 → 未命中：信号量限制在途请求 ≤ 8，排队超时返回繁忙
  ▼
固定流程（LangGraph）
  generate → execute → check ──(报错 / 空结果 / 取值不存在)──→ correct，最多 3 次
  ▼
只读执行：sqlglot 只放行单条 SELECT → mode=ro 只读连接 → SQLite 授权回调
模型：Claude（Haiku / Sonnet / Opus）、DeepSeek 和通义千问（OpenAI 兼容接口）、Ollama 本地模型
MCP Server：list_tables / describe_table / search_values / run_sql，只读
```

设计决策记录在 `docs/adr/`：[数据来源与评测口径](docs/adr/001-数据来源与评测口径.md)、[改进实验设计](docs/adr/002-改进实验设计.md)、[国产模型与中文评测](docs/adr/003-国产模型与中文评测.md)、[智能路由](docs/adr/004-智能路由.md)、[行列级权限](docs/adr/005-行列级权限.md)。

## 评测结果

BIRD Mini-Dev 500 题，指标是执行准确率（EX）。评测器和官方 `evaluation_ex.py` 逐题对齐过，三组自检共 1500 次判分没有不一致。

| 配置 | Sonnet | Haiku | 备注 |
|---|---|---|---|
| E0 基线（官方提示词） | 71.8% | 63.0% | |
| E1 提示缓存 | 70.8% | 64.2% | 准确率不变，Sonnet 成本降 51% |
| E2 schema 增强 | 71.6% | 65.4% | 不显著 |
| E3 取值核对 | 72.8% | 67.0% | 不显著 |
| E4 执行纠错（最终） | 72.4% ± 0.35 | 68.0% ± 0.72 | 3 次均值，Haiku 相对 E0 p=0.008 |
| Agent（模型自己调工具） | 72.0% | 65.8% | 与 E4 无差别，更贵更慢 |
| schema 检索 | 70.0%（373 题） | | 显著变差，成本 2.9 倍 |

模型对比（E4，500 题）：

| 模型 | EX | 单题成本 | P95 延迟 |
|---|---|---|---|
| Haiku | 67.4%（3 次均值 68.0%） | $0.0002 | 4.7 s |
| Sonnet | 72.0%（3 次均值 72.4%） | $0.0035 | 8.6 s |
| qwen3.7-plus | 68.0% | $0.0004 | 6.7 s |
| deepseek-flash | 66.8% | $0.0013 | 28.5 s |
| qwen2.5-coder:7b（本地） | 53.8% | 0 | |
| Opus | 86.4% | $0.0057 | 4.2 s |

Opus 的分数不作为能力结论：500 条预测里有 241 条和标准答案逐字相同，疑似训练数据污染（问题日志 P7）。国产模型的成本按标价估算。

路由：

| 方案 | EX | 成本（全用 Sonnet = 100%） |
|---|---|---|
| 全用 Haiku | 68.0% | 7% |
| 路由（实跑） | 69.6% | 14% |
| 全用 Sonnet | 72.4% | 100% |
| 理想路由（预先知道哪题会错） | 77.5% | 42% |

Haiku 的错题大多执行成功、没有报错，运行时看不出来，所以路由只追回了差距的三分之一。

口径消融（模拟电商库 40 题）：口径写清楚时 Haiku 95.8%、Sonnet 97.5%、DeepSeek 95.0%、qwen 92.5%；去掉口径说明后是 65.8%、64.2%、62.5%、55.0%。

## 行列级权限

在 SQL 执行前按角色限制能看到的列和行，叠加在只读防护之上（[ADR 005](docs/adr/005-行列级权限.md)）。电商库有 3 个角色：分析师看全部；运营看不到成本价和客户姓名；上海区域经理在运营的基础上，只能看上海的用户、订单和售后。

- 主防线是语法树改写：sqlglot 解析后，把每一处受限表引用（包括别名、子查询、JOIN、UNION、CTE 内部）换成只含可见列、带行过滤的子查询。直接引用敏感列会被拒绝并说明原因，无法解析的 SQL 一律拒绝。
- 兜底是 SQLite 授权回调：每读一列都会检查，就算改写有漏洞，敏感列也读不出来。
- 表结构和样例只展示可见部分，`search_values` 不能用来探测敏感值，缓存 key 带角色。
- 角色由请求头 `X-Role` 传入，只用于演示，生产环境应由网关按登录身份填写。MCP 的角色由启动环境固定，模型改不了。

| 评测 | 带权限说明 | 不带权限说明 |
|---|---|---|
| 越权次数 | 0 / 102 | 0 / 68 |
| 需要无权限数据时如实告知 | 16 / 16 | 8 / 16 |
| 区域经理的回答说明了权限范围 | 26 / 26 | 4 / 26 |

越权为 0 靠的是代码。能不能如实告知，取决于有没有把权限范围告诉模型：不告诉的话，区域经理问"北京用户下了多少单"会得到"0 单"，看起来像事实（问题日志 P19）。

## 并发与多用户

- 限流：前端为每个浏览器生成匿名 id，放在 `X-User-Id`（没有时按 IP），每分钟固定窗口计数，Redis 里用 INCR 加 EXPIRE NX 事务。上限 20 时，第 21 次返回 429。
- 隔离：会话 key 和会话锁都带用户 id，查询缓存按库共享。
- 并发保护：只有未命中缓存的请求占名额，在途最多 8 个，排队超过 10 秒返回"系统繁忙"。名额满时先推一个"排队等待"事件，首个事件延迟 P50 从 10.55 s 降到 0.016 s（问题日志 P14）。

压测用模拟模型（每次调用固定 1.5 秒，不调 API）：缓存命中路径 100 并发 P95 197 ms、0 错误；未命中路径吞吐 2.6–2.7/s，正好是名额 8 除以单题 3 秒的理论上限，50 并发时 36% 的请求被拒，下游没有被打穿。

```bash
DATAPILOT_MODEL=simulated DATAPILOT_SIM_LATENCY_S=1.5 REDIS_URL=redis://localhost:6379/2 \
  uv run uvicorn datapilot.api:app --port 8010
uv run python scripts/load_test.py --base http://127.0.0.1:8010
```

信号量和会话锁只在单进程内有效，多 worker 部署时要换成 Redis 上的分布式实现。

## 运行

需要 Python 3.12（用 uv 管理）、Node 20+，Redis 可选。

```bash
uv sync
npm --prefix web install
uv run python scripts/download_data.py      # BIRD Mini-Dev，数据库约 801MB
uv run python scripts/gen_shop_ops.py       # 可选：模拟电商库 shop_ops（口径消融、行列级权限演示用）
uv run python scripts/add_database.py shop_ops data/source/shop_ops.sqlite
cp .env.example .env                        # 填 ANTHROPIC_API_KEY；不填也能启动，问数会报「未设置 ANTHROPIC_API_KEY」

uv run pytest -q
uv run uvicorn datapilot.api:app --port 8000
npm --prefix web run dev                    # http://localhost:3000
```

后端接口都在 `/api/*` 下（健康检查 `GET /api/health`）；前端通过环境变量 `NEXT_PUBLIC_API_BASE` 找后端，默认 `http://localhost:8000`。没有 Redis 时服务自动退回内存存储，`uv run pytest -q` 会跳过 6 个需要 Redis 的测试，其余照常通过。

Docker：先下载数据，再 `docker compose up --build`，打开 http://localhost:3000。数据目录只读挂载，不打进镜像。

跑评测（不加 `--yes` 只打印预估花费）：

```bash
uv run python evals/runner.py --models sonnet,haiku --pipeline e4_correct --max-cost 3 --yes
uv run python evals/compare.py <run_a> <run_b> --model haiku
uv run python scripts/build_dashboard.py
```

## MCP Server

`.mcp.json` 已配好，任何 MCP 客户端都能接入，接入后可以直接问"superhero 库里各出版商有多少英雄"。不接客户端也能验证：

```bash
uv run python scripts/mcp_smoke.py   # 拉起服务，列工具、查询，确认 DROP 被拒
```

![通过 MCP 查询](docs/images/mcp-client.png)

删表请求在数据库一侧就会被拒：sqlglot 只放行单条 SELECT，连接是只读的，授权回调只放行读操作。不依赖模型自己拒绝，也不依赖客户端的安全检查。

## 其他文档

- [docs/numbers.md](docs/numbers.md)：全部数字和运行 ID
- [docs/问题日志.md](docs/问题日志.md)：开发中遇到的问题、排查过程和修复
- [docs/failure/判定表.md](docs/failure/判定表.md)：50 道失败题的判定
- [docs/roi.md](docs/roi.md)：ROI 测算
- [docs/接入新数据库.md](docs/接入新数据库.md)：接入一个新库并评测的步骤
- [CHANGELOG.md](CHANGELOG.md)

## 局限

- 数据来自公开基准 BIRD Mini-Dev 和一个固定种子生成的模拟电商库，不是真实业务数据。
- 缓存和压测的请求是构造的，压测用的是模拟模型，客户端和服务端在同一台机器上。
- 用户身份是前端生成的匿名 id，换一个就能绕过限流。真正防滥用需要网关校验身份。
- "真实准确率约 90%"是根据 50 道人工判定外推的估计值。
- 国产模型走 JSON 模式，Claude 用结构化输出，提示方式不完全一样；国产模型的结论来自单次运行。

## 数据与许可

代码使用 MIT 许可。BIRD Mini-Dev（https://github.com/bird-bench/mini_dev）使用 CC BY-SA 4.0，数据库和题目不随代码分发，由 `scripts/download_data.py` 下载；`runs/` 和 `web/src/data/eval.json` 里含部分题目原文和模型输出，同样按 CC BY-SA 4.0 署名使用。`third_party/bird_eval/` 是官方评测脚本原件，只用于逐题对照。
