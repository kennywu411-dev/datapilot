# DataPilot：NL2SQL 智能问数

**实验驱动的 Workflow / Agent 选型 + 带限流与并发保护的后端（压测验证）。**

用中文提问，系统读表结构、写 SQL、在只读库上执行，出错时带着报错自动修正，最后给出答案、SQL、可排序表格和自动选择的图表，支持追问。每一个设计选择都有对照实验：固定流程还是 Agent、要不要 Schema 检索、用哪个模型、要不要路由、口径文档值多少——全部在同一批题上配对检验，数字和运行 ID 见 [docs/numbers.md](docs/numbers.md)。

![演示：提问 → 流式步骤 → SQL → 图表 → 追问 → 评测看板](docs/images/demo.gif)

（[高清 MP4](docs/images/demo.mp4)，由 `scripts/record_demo.py` 自动录制）

## 选型结论（每条都有对照实验）

| 问题 | 结论 | 证据 |
|---|---|---|
| 固定流程还是 Agent？ | **固定流程**：准确率相同，Agent 成本 +56%、延迟 +55% | BIRD 500 题，Sonnet 72.0% vs 72.0%，p=1.0 |
| 哪些改进有用？ | 取值核对 + 执行纠错：Haiku **63.0% → 68.0%**（p=0.008）；对 Sonnet 不显著 | 逐项消融，3 次运行 |
| 要不要 Schema 检索？ | **不要**（schema 放得下时）：显著变差 p=0.013，成本 ×2.9 | 373 题配对 |
| 用哪个模型？ | Sonnet 72.4%；国产 **qwen3.7-plus 68.0% ≈ Haiku 67.4%**（p=0.81），成本 $0.0004/题，国内部署首选 | 500 题，含 DeepSeek 66.8% |
| 值得做智能路由吗？ | Haiku 先答、触发纠错才升级 Sonnet：**69.6%，成本为全 Sonnet 的 14%**；只追回差距的 1/3 | 离线回放 3 组 + 实跑一致 |
| 用中文提问会掉分吗？ | **不会**：中英文无显著差别（Haiku 64→69%，Sonnet 77→72%） | BIRD 同批 100 题配对 |
| 企业问数的瓶颈在哪？ | **口径和元数据**：去掉口径说明，所有模型从 93–98% 掉到 55–66%，换 Sonnet 补不回来 | 模拟电商库 40 题消融 |
| 口径怎么交给模型？ | **指标语义层，口径注入**：用户不写口径，系统按同义词匹配指标字典、把口径带给模型，Haiku **65.8% → 92.5%**、Sonnet 64.2% → 95.0%（每次 p ≤ 0.002）；字典定稿后再写的 19 道留出题 60–65% → 100%。让语义层直接生成 SQL 反而低 2 题（映射错了没有纠错机会） | 去口径 40 题 + 留出 19 题，3 次运行配对 |
| 普通员工怎么防止查到敏感数据？ | **代码强制的行列级权限**：语法树改写 + 授权回调兜底，越权 **0 / 170** 次（审查中修了两处同名 CTE 绕过）；把权限范围告诉模型后，如实告知无权限 8/16 → **16/16** | 46 个绕过测试 + 3 角色评测 |
| 标准答案可信吗？ | 失败题里 **56% 是标准答案错**（审计论文报告 52.8%） | 50 道人工判定 |

| Haiku 执行准确率 | 口径消融 | 缓存命中延迟（P50） |
|---|---|---|
| **63.0% → 68.0%** | **96% → 66%** | **0.9 ms**（未命中 3.71 s） |
| 取值核对 + 执行纠错，McNemar p=0.008，3 次均值 | 去掉口径说明后的 Haiku（3 次均值），模拟电商库 | 构造请求下重复问题命中 98.3% |

![问数界面：柱状图、追问与饼图](docs/images/chat.png)

![评测看板](docs/images/eval.png)

![失败分析：单题回放](docs/images/eval-failure.png)

## 诚实边界

- 数据库和问题来自公开基准 **BIRD Mini-Dev**，不是某家公司的生产数据；个人项目，没有真实用户。
- **Opus 的分数疑似受训练数据污染**：500 条预测里 241 条与标准答案逐字相同，不当作能力结论（`docs/问题日志.md` P7）。
- 缓存实测的请求分布是**构造的**（140 道不重复 + 60 次重复），不是真实流量。
- 并发压测用的是**模拟模型**（固定延迟、不调 API），测的是服务端自身；客户端与服务端同机，吞吐在两次运行间相差近一倍。用户身份是前端生成的匿名 id，客户端每次换一个就能绕过限流；要真正防滥用需要网关校验身份。
- "真实准确率约 90%" 是由 50 道人工判定外推的**估计**，不是实测。
- **电商经营分析用的是模拟数据**：`scripts/gen_shop_ops.py` 固定种子生成，不代表任何真实公司；40 道题起草后逐条执行核对、本人确认。
- 指标字典是看过原 40 题的口径说明之后写的，原题上的提升有拟合成分；字典定稿后另写了 19 道留出题，同样从 60–65% 提到 100%，但题少、已到天花板，只能说明没有只拟合原题。
- 中文问法先机翻再由本人校对；首轮有 3 条译文偏离原意，修正后重跑（`docs/问题日志.md` P18）。
- 国产模型走 OpenAI 兼容接口的 JSON 模式，Claude 用结构化输出 + adaptive thinking，提示方式不完全相同；各模型主要结论来自单次运行，Haiku / Sonnet 有 3 次重复。

## 架构

```
浏览器（Next.js + shadcn/ui + ECharts，深色优先，中文界面）
  │  SSE：步骤进度 → SQL → 表格 → 图表 → 回答（15 秒心跳）
  ▼
FastAPI ─── Redis：会话（按用户隔离，最近 3 轮）+ 查询缓存（库 + 配置版本 + 模型 + 规范化问题）
  │            + 限流计数（每用户每分钟）；连不上 Redis 时自动退回内存
  │  限流 429 → 缓存命中直接返回 → 未命中：信号量（在途 ≤ 8，排队超时报繁忙）
  ▼
固定流程（LangGraph 状态机）
  generate → execute → check ──(报错 / 空结果 / 取值不存在)──→ correct ─┐
                 ▲                                                       │
                 └───────────────────── 最多 3 次 ───────────────────────┘
  ▼                                         ▲
只读执行层                              LLM 接口：ClaudeLLM（Haiku / Sonnet / Opus）
  1. sqlglot 只放行单条 SELECT                      OpenAICompatLLM（DeepSeek、通义千问）
                                                    LocalLLM（Ollama，qwen2.5-coder:7b）
                                                    RoutedPipeline：Haiku 先答，失败信号升级 Sonnet
  2. mode=ro 只读连接
  3. SQLite 授权回调只放行读操作                 只读 MCP Server（list_tables / describe_table /
  + 超时、行数上限                                 search_values / run_sql），可接入任意 MCP 客户端

评测：BIRD Mini-Dev 500 题 → runner（断点续跑、花费硬上限）→ 与官方逐题对齐的 EX → 看板
```

关键取舍都写成了设计决策记录：[ADR 001 数据来源与评测口径](docs/adr/001-数据来源与评测口径.md)、[ADR 002 D2 实验设计](docs/adr/002-D2实验设计.md)、[ADR 003 国产模型与中文评测](docs/adr/003-国产模型与中文评测.md)、[ADR 004 智能路由](docs/adr/004-智能路由.md)、[ADR 005 行列级权限](docs/adr/005-行列级权限.md)。

## 评测结果

BIRD Mini-Dev 500 题，指标为执行准确率（EX），评测器与官方 `evaluation_ex.py` 逐题对齐（三组自检 1500 次判分 0 处不一致）。

| 配置 | Sonnet | Haiku | 说明 |
|---|---|---|---|
| E0 基线（官方提示词） | 71.8% | 63.0% | |
| E1 提示缓存 | 70.8% | 64.2% | 准确率不变，Sonnet 成本 −51% |
| E2 + Schema 增强 | 71.6% | 65.4% | 不显著 |
| E3 + 值核对 | 72.8% | 67.0% | 不显著 |
| **E4 + 执行纠错（最终）** | **72.4% ± 0.35** | **68.0% ± 0.72** | 3 次均值；Haiku 相对 E0 p=0.008 |
| Agent（模型自主调用工具） | 72.0% | 65.8% | 与 E4 无差别，成本 +56%、延迟 +55% |
| Schema 检索（消融） | 70.0%（373 题） | | 显著变差 p=0.013，成本 ×2.9 |

结论：改进对较弱模型显著；这个场景不需要自主 Agent；schema 放得下时不需要检索。

### 模型选型（最终配置 E4，500 题）

| 模型 | EX | 单题成本 | P95 延迟 | vs Haiku | vs Sonnet |
|---|---|---|---|---|---|
| Haiku | 67.4%（3 次均值 68.0%） | $0.0002 | 4.7 s | — | — |
| Sonnet | 72.0%（3 次均值 72.4%） | $0.0035 | 8.6 s | — | — |
| **qwen3.7-plus**（通义千问） | **68.0%** | $0.0004 | 6.7 s | 打平 p=0.81 | 低 p=0.042 |
| deepseek-flash | 66.8% | $0.0013 | 28.5 s | 打平 p=0.80 | 低 p=0.003 |
| qwen2.5-coder:7b（本地） | 53.8% | 本地 | — | 低 p<0.001 | — |
| Opus（疑似污染） | 86.4% | $0.0057 | 4.2 s | — | — |

国产模型经 OpenAI 兼容接口接入，流程代码不改；逐字复现标准答案的题数低于 Claude，没有污染迹象。国产模型的成本按标价估算，未与账单逐笔核对（实际可能更低）；路由成本占比的分母是 Sonnet 三次运行的平均成本。

### 智能路由（Haiku 先答，触发纠错才升级 Sonnet）

| 方案 | EX | 成本 / 全 Sonnet |
|---|---|---|
| 全用 Haiku | 68.0% | 7% |
| **路由 r1（实跑）** | **69.6%** | **14%** |
| 全用 Sonnet | 72.4% | 100% |
| 理想路由（预知哪题会错） | 77.5% | 42% |

实跑与离线回放（69.4%）一致。Haiku 的错题大多"执行成功、没有报错"，运行时信号看不出来，所以只追回差距的 1/3；提升空间在更好的"会不会错"判断器。

### 中文提问与业务口径

- **中文 vs 英文**（BIRD 同批 100 题，配对）：Haiku 64% → 69%（p=0.18），Sonnet 77% → 72%（p=0.27），无显著差别；29 道取值映射题持平。
- **电商经营分析**（模拟库 40 题）：口径写清楚时 Haiku 95.8%、Sonnet 97.5%、DeepSeek 95.0%、qwen 92.5%；**去掉口径说明**后分别为 65.8%、64.2%、62.5%、55.0%。强模型不更会猜口径——企业问数的瓶颈在语义层，不在模型档位。

全部数字和运行 ID 见 [docs/numbers.md](docs/numbers.md)。

## 指标语义层与数据质量

口径不靠用户在问题里写，由指标字典统一维护（[ADR 006](docs/adr/006-指标语义层与数据质量.md)）：

| 组成 | 做法 |
|---|---|
| 指标字典 | `src/datapilot/metrics/shop_ops.yaml`：16 个指标（11 个可直接生成 SQL、5 个比率 / 复购类复合指标）、7 个业务术语、13 个维度、全局约定、表关联；每个指标写口径、计算片段、过滤条件、同义词，加载时校验列存在 |
| 匹配 | 同义词最长匹配 + 字二元组模糊检索兜底；离线标注评测精确率 94.9%、召回率 92.5%（留出题 94.1% / 84.2%） |
| 接入问数 | 匹配到的口径并入提示（默认）；也可让语义层直接生成 SQL（`sem_compile`），实验显示更差，不作为默认 |
| 与权限兼容 | 指标依赖受限列（毛利依赖成本价）时不给计算片段、语义层直出直接拒绝；最终 SQL 照常经过行列级权限改写和授权回调 |
| 指标目录页 | 问数界面"指标目录"：口径、计算 SQL、维度、同义词、血缘（依赖的表和列），切换角色可看到哪些指标不能用；问数回答下方显示本题用到的指标，点开看口径 |
| 数据质量 | `uv run python -m datapilot.quality shop_ops`：主键、业务键重复、空值、枚举、数值范围、外键一致性、行级业务规则、新鲜度共 42 项；注入 13 类缺陷全部检出、干净库 0 误报；界面"数据质量"页展示报告 |

| 去口径 40 题（3 次均值） | 无语义层 | 口径注入（默认） | 语义层直出 |
|---|---|---|---|
| Haiku | 65.8% | **92.5%** | 87.5% |
| Sonnet | 64.2% | **95.0%** | 90.0% |

## 行列级权限

按角色限制可见的列和行，在 SQL 执行前由代码强制，叠加在三道只读防线之上（[ADR 005](docs/adr/005-行列级权限.md)）。电商库定义了 3 个角色：分析师（全部可见）、运营（看不到成本价和客户姓名）、上海区域经理（同运营，且只看上海用户及其订单、售后）。

| 层 | 做法 |
|---|---|
| 语法树改写（主防线） | sqlglot 解析后，把每一处受限表引用（别名、子查询、JOIN、UNION、`main.表名`、CTE 内部）替换成"只含可见列、带行过滤"的子查询；显式引用敏感列直接拒绝并说明原因；元数据表、无法解析的 SQL 一律拒绝 |
| 授权回调（兜底） | SQLite 每读一列都会询问，敏感列直接拒绝——即使改写有漏洞也读不出来 |
| 展示与探测 | 表结构只给可见列（敏感列标"无权访问"）、样例只取可见行；`search_values` 和值核对不能探测敏感值；缓存 key 带角色 |
| 角色来源 | 问数接口用请求头 `X-Role`（演示用，生产须由网关按登录身份填写）；MCP 由启动环境固定，工具参数里不给角色，模型无法自己提权 |

| 评测（`rls-eval-*` / `rls-nonote-*`） | 带权限说明 | 去掉权限说明 |
|---|---|---|
| 越权次数 | **0 / 102** | **0 / 68** |
| 需要无权限数据时如实告知 | **16 / 16** | 8 / 16 |
| 区域经理的回答说明了权限范围 | **26 / 26** | 4 / 26 |

越权为 0 来自代码强制；能不能如实告知取决于是否把权限范围告诉模型——不告诉时，区域经理问"北京用户下了多少单"会得到"0 单"，像事实一样（问题日志 P19）。问数界面左侧可以切换角色演示。

## 并发与多用户

| 机制 | 做法 | 实测（`load-20261023-140716`，模拟模型） |
|---|---|---|
| 按用户限流 | 前端每个浏览器生成匿名 id 放在 `X-User-Id`（没有时按 IP），每分钟固定窗口，Redis INCR + EXPIRE NX 事务计数 | 上限 20 时第 21 次起 429 |
| 多用户隔离 | 会话 key 与会话锁带用户 id；查询缓存按库共享 | 单元测试：同一 session_id，另一个用户读不到追问上下文 |
| 并发保护 | 未命中缓存才占名额，在途 ≤ 8，排队超过 10 秒返回"系统繁忙"；拿到名额后再查一次缓存，防止同一问题重复调用模型 | 吞吐 2.6–2.7/s，等于理论上限；50 并发时 36% 被拒，下游没有被打穿 |
| 排队反馈 | 名额满时先推"排队等待"事件（P14） | 首个事件 P50：10.55 s → 0.016 s |
| 缓存命中路径 | 不调模型 | 100 并发 P95 197 ms、0 错误 |

```bash
# 压测（不花钱）：先用模拟模型启动服务，再跑脚本
DATAPILOT_MODEL=simulated DATAPILOT_SIM_LATENCY_S=1.5 REDIS_URL=redis://localhost:6379/2 \
  uv run uvicorn datapilot.api:app --port 8010
uv run python scripts/load_test.py --base http://127.0.0.1:8010
```

![限流时前端原样展示后端提示](docs/images/ratelimit-429.jpg)

信号量和会话锁只在单进程内有效；多 worker 部署时并发上限变成 进程数 × 8，需要换成 Redis 侧的分布式信号量。

## 复现

需要：macOS / Linux，Python 3.12（由 uv 管理），Node 20+，可选 Redis。

```bash
# 1. 安装依赖
uv sync
npm --prefix web install

# 2. 下载 BIRD Mini-Dev（题目文件 + 数据库完整包约 801MB）
uv run python scripts/download_data.py

# 2b. 可选：生成并接入模拟电商库 shop_ops（口径消融与行列级权限演示用，固定种子不花钱）
uv run python scripts/gen_shop_ops.py
uv run python scripts/add_database.py shop_ops data/source/shop_ops.sqlite

# 3. 填 API key（不填也能启动，但问数会报「未设置 ANTHROPIC_API_KEY」）
cp .env.example .env   # 编辑 .env，填 ANTHROPIC_API_KEY

# 4. 跑测试与评测自检（不花钱）
uv run pytest -q
uv run python scripts/check_gold.py
uv run python evals/runner.py --models gold --pipeline e4_correct

# 5. 启动（Redis 可选，没有时自动用内存）
redis-server --save "" &          # 可选
uv run uvicorn datapilot.api:app --port 8000
npm --prefix web run dev          # 打开 http://localhost:3000
```

后端接口都在 `/api/*` 下（健康检查 `GET /api/health`）；前端通过环境变量 `NEXT_PUBLIC_API_BASE` 找后端，默认 `http://localhost:8000`。没有 Redis 时服务自动退回内存存储，`uv run pytest -q` 会跳过 6 个需要 Redis 的测试，其余照常通过。

也可以用 Docker 一键启动（后端 + 前端 + Redis；数据目录只读挂载，不进镜像）：

```bash
uv run python scripts/download_data.py   # 先下载数据
docker compose up --build                # 打开 http://localhost:3000
```

macOS 上还可以生成一个双击启动的应用（没运行就启动并打开页面；已运行就选择打开页面或关闭服务）：

```bash
bash scripts/app/make_app.sh             # 生成 ~/Applications/DataPilot.app，可拖进程序坞
```

终端里也可以直接用 `bash scripts/app.sh start | stop | status`。

> 前端构建时把后端地址写成 `http://localhost:8000`，后端 CORS 默认只放行 `localhost:3000`，所以默认只能在运行 Docker 的这台机器上访问；要从别的机器访问，用 `--build-arg NEXT_PUBLIC_API_BASE=...` 重建前端，并设置环境变量 `DATAPILOT_CORS_ORIGINS`。
>
> 已实测（colima + Docker 29，2026-10-22）：首次构建约 5 分钟；镜像后端 783MB、前端 281MB（Next.js standalone）；容器内问数、SSE、Redis 会话与缓存命中均正常。

跑付费实验（不加 `--yes` 只打印预估花费）：

```bash
uv run python evals/runner.py --models sonnet,haiku --pipeline e4_correct --max-cost 3 --yes
uv run python evals/compare.py <run_a> <run_b> --model haiku   # 配对检验
uv run python scripts/build_dashboard.py                        # 重新生成看板数据
```

## 在 MCP 客户端里用只读 MCP Server

项目根目录的 `.mcp.json` 已配置好。在本目录打开支持项目级 MCP 配置的客户端，批准 `datapilot` 服务后即可直接问数据库，例如"用 datapilot 查一下 superhero 库里各出版商有多少英雄"。不打开客户端也能验证：

```bash
uv run python scripts/mcp_smoke.py   # 按 .mcp.json 的命令拉起服务，列工具、查询、验证 DROP 被拒
```

![在 MCP 客户端里通过 datapilot MCP 查询](docs/images/mcp-client-query.png)

要求删表时的表现（下图）：模型自己拒绝了，没有调用工具。删表的防护是分层的，越往后越不依赖模型：

| 层 | 谁负责 | 能否依赖 |
|---|---|---|
| 1. 模型拒绝 | 模型自身判断（下图） | 不能：可能被数据里的提示注入带偏 |
| 2. Agent 产品的安全检查 | 客户端的权限确认拦截破坏性调用 | 不能：别的 Agent 产品不一定有 |
| 3. MCP 服务三道防线 | sqlglot 单条 SELECT 白名单 → 只读连接 → SQLite 授权回调 | 能：在数据库一侧，冒烟脚本实测 DROP 返回 `rejected` |

![要求删表时模型拒绝](docs/images/mcp-drop-refused.png)

## 接入新数据库

流程写成了 Agent Skill：[skills/chatbi-add-database/SKILL.md](skills/chatbi-add-database/SKILL.md)（复制库 → 补列说明 → 写评测题 → 空跑校验 → 报预算后评测 → 失败分析 → 登记）。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/numbers.md](docs/numbers.md) | 全部数字与运行 ID |
| [docs/问题日志.md](docs/问题日志.md) | 20 个真实问题（现象、排查、根因、解决、前后对比） |
| [docs/badcases.md](docs/badcases.md) | 3 个 bad case |
| [docs/failure/判定表.md](docs/failure/判定表.md) | 50 道失败题的判定标准与逐题理由 |
| [docs/roi.md](docs/roi.md) | ROI 测算框架（实测与假设分开、盈亏平衡、敏感性） |
| [docs/SOP.md](docs/SOP.md) | 从问题日志提炼的评测与工程经验 |
| [CHANGELOG.md](CHANGELOG.md) | 各版本新增、修复与数字变化 |

## 数据与许可

- BIRD Mini-Dev：https://github.com/bird-bench/mini_dev ，许可 **CC BY-SA 4.0**。数据库与题目不随代码分发，由 `scripts/download_data.py` 下载。
- `third_party/bird_eval/` 为官方评测脚本原件，仅用于逐题对照。
