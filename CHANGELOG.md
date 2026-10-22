# CHANGELOG

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
