# CHANGELOG

## v1.1 — 补全：本地模型全量、docker compose、高清截图（2026-10-22）

### 新增
- 本地模型 qwen2.5-coder:7b 跑完 500 题全量，接入看板模型对比（runs/MANIFEST.json 的 local_model_500）
- docker compose 一键启动（后端 + 前端 + Redis），实测通过；前端镜像改 Next.js standalone（1.44GB → 281MB）
- README 截图换成 2 倍清晰度（scripts/screenshots.py：Chrome 无头模式 + CDP 自动问数后截图）

### 数字
- 本地 qwen2.5-coder:7b 500 题 EX 53.8%（49.4–58.1%），比 Haiku 68.0%（3 次均值）低约 14 个百分点；与 Haiku 首次运行逐题配对（67.4% vs 53.8%）p<0.001；语法错误 8.2%，P50 5.5 s（qwen500-20261022-215451-f53b）
- 此前 100 题抽样 56%（与 Haiku 差距不显著），全量后结论变为显著

## v1.0 — D5：评测看板、README、交付（2026-10-22）

### 新增
- 评测看板页：逐项改进、模型对比、失败分析与单题回放、评测可信度（数据由 runs/ 现算，运行 ID 集中在 runs/MANIFEST.json）
- README：截图、核心数字、架构、结果、复现、诚实边界、数据许可
- ROI 测算框架（实测与假设分开、盈亏平衡、敏感性分析）
- Agent Skill（接入新库并评测）+ scripts/add_database.py，并用它实际接入 opspilot 库；SOP
- MCP 接入（项目级 .mcp.json）与冒烟脚本
- 代码包脚本（文件清单检查 + 密钥扫描 + 解压自检）

### 修复
- 代码包改 tar.gz：macOS 自带 unzip 解压中文文件名失败（P12）
- .gitignore 的 data/ 误伤看板数据，干净检出构建失败（P13）
- 代码审查 8 项：SQLite 一致性拷贝、密钥规则覆盖、续跑题集校验等

### 验证
- 按 README 在干净目录从头复现：依赖安装、下载、154 个测试、评测自检、全管线空跑 99.6%、前端构建

## v0.4 — D4：前端与 Redis（2026-10-22）

### 新增
- 问数服务：流式事件（步骤 → SQL → 表格 → 图表 → 回答）、追问记忆、查询缓存、规则选图
- Redis / 内存存储（同一接口、自动退回）；FastAPI SSE 接口（15 秒心跳）
- 前端：Next.js + shadcn/ui + ECharts，深色优先、中文界面；流式过程、可排序表格、SQL 高亮复制、图表切换、库与表结构浏览、错误与断线重试
- 缓存实测脚本、回答阶段注入测试；pre-commit 增加前端 lint + tsc

### 修复
- 缓存 key 规范化只动引号外（P10）；回答阶段告知总行数（P11）
- 浏览器试用发现的展示问题：默认视图、整数年份不出图、x 轴标签、图表截断未注明
- 代码审查 9 项：最终 SQL 执行两次、计时器泄漏、失败轮次不进会话、会话并发覆盖等

### 数字
- 缓存（构造分布）：命中率 29.5%，重复命中 98.3%，命中 P50 0.9 ms vs 未命中 3.71 s，节省 29.5%
- 回答阶段注入：0 泄露、0 劫持

## v0.3 — D3：失败分析、稳定性、模型选型（2026-10-22）

### 新增
- LocalLLM（Ollama，qwen2.5-coder:7b），统一 LLM 接口，流程代码不改
- 50 道失败题分层抽样 + 判定标准 + 预分类，本人判定（`docs/failure/`）
- 3 个 bad case 口述版（`docs/badcases.md`）、检查点 2 讲解与参考答案

### 修复
- 代码审查 6 项：本地模型上下文溢出会静默截断 schema、max_tokens 重试抽成公共函数、
  runner 校验模型名与本地 + agent 组合、本地模型共用 HTTP 客户端

### 数字
- 失败分析：标准答案错 56%（CI 42–69%），模型错 4%；外推真实 EX 约 90%（估计）
- 稳定性：Sonnet 72.4% ± 0.35，Haiku 68.0% ± 0.72（E4 × 3）
- Opus E4 86.4%（与基线无差别，成本 −45%）
- 同 100 题：本地 7B 56% / Haiku 66% / Sonnet 76% / Opus 88%

## v0.2 — D2：改进实验、Agent 对比、注入测试（2026-10-22）

### 新增
- 固定流程（LangGraph 状态机）：Schema 增强、值核对、执行反馈纠错（≤3 次），schema 进缓存前缀
- Agent 版本：模型自主调用 search_values / run_sql，submit_answer 交卷
- Schema 检索消融（BM25 + 外键邻表）
- 配对比较工具 `evals/compare.py`（McNemar 精确检验、纠错改对/改错统计）
- 提示注入测试（数据库副本埋 6 类指令）
- 检查点 1 讲解与参考答案

### 修复
- 提示缓存从未命中：Sonnet 成本 -51%（P4）
- Sonnet 偶发生成失控：max_tokens 16000 → 4096 + 重试一次；结构化输出改为自行解析（P8）
- 代码审查 9 项：纠错失败退回上一版、值核对别名作用域、示例值 SQL 字面量、门禁覆盖最终配置等

### 数字
- Haiku：63.0% → 67.4%（E4，McNemar p=0.008），成本 -35%
- Sonnet：71.8% → 72.0%（不显著）；Agent 同为 72.0%，但成本 +56%、延迟 +55%
- Schema 检索：EX 显著下降（p=0.013），成本 ×2.9（P9）
- 注入测试：40 次运行 0 次被带偏
- 单次运行噪声：同等信息的两次运行，Sonnet 有 35 题翻转

## v0.1 — D1：数据、只读安全层、评测与基线（2026-10-22）

### 新增
- BIRD Mini-Dev 下载与统计：题目用 HF 版，数据库用官方完整包（P1）
- 只读 SQL 执行层：sqlglot 单条 SELECT 白名单 + `mode=ro` + 授权回调 + 超时 + 行数上限
- EX 评测器：与官方 `evaluation_ex.py` 逐题对齐（1500 次判分 0 处不一致），预测 SQL 走只读连接
- 固定流程基线（官方提示词格式 + 结构化输出）与 runner（分层抽样、断点续跑、花费硬上限）
- 只读 MCP Server：list_databases / list_tables / describe_table / search_values / run_sql
- pre-commit：ruff + pytest + 离线门禁（含打乱顺序与假 Agent 对照）+ Haiku 冒烟门禁

### 修复
- 评测超时口径：预测与标准 SQL 共用 30 秒（P2）
- 离线门禁测不出顺序 bug（P5）
- 只读连接拦不住 ATTACH / PRAGMA / 临时表，加授权回调；TokenError 未捕获（P6）
- 代码审查 10 项：zip 路径穿越、LIKE 转义、分层抽样总数、工具元数据缓存等

### 数字
- 全量基线 EX：Haiku 63.0% / Sonnet 71.8% / Opus 86.6%（baseline-20261022-144835-8314，花费 $8.06）
- 空 Agent 下限 3.2%，EX 理论上限 99.6%
- Opus 疑似数据污染：241/500 逐字复现标准答案（P7）
