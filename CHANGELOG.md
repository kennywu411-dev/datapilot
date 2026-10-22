# CHANGELOG

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
