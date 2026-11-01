"""DataPilot 只读 MCP Server（stdio）。

把 tools.py 的数据库工具按 MCP 协议暴露，任何 MCP 客户端都能调用。
所有工具只读：run_sql 走 sql_guard 的分层防御。

启动：uv run python -m datapilot.mcp_server
调试：npx @modelcontextprotocol/inspector uv run python -m datapilot.mcp_server

行列级权限（v1.4）：角色由启动时的环境变量 DATAPILOT_ROLE 固定（在 .mcp.json 的 env 里配），
工具参数里不提供角色，否则模型可以自己传一个更高的角色给自己提权。
"""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer

from datapilot import tools

ROLE = os.environ.get("DATAPILOT_ROLE") or None

server = MCPServer(
    "datapilot",
    instructions=(
        "只读 SQLite 问数工具。建议顺序：list_databases → list_tables → describe_table "
        "→ search_values（确认真实取值）→ run_sql。只允许单条 SELECT。"
    ),
)


@server.tool()
def list_databases() -> list[str]:
    """列出可查询的数据库 db_id。"""
    return tools.list_databases()


@server.tool()
def list_tables(db_id: str) -> list[dict]:
    """列出数据库里的表和各表列数。"""
    return tools.list_tables(db_id)


@server.tool()
def describe_table(db_id: str, table: str) -> dict:
    """表结构：列名、类型、主键、外键、列说明和取值说明、3 行样例。"""
    return tools.describe_table(db_id, table, ROLE)


@server.tool()
def search_values(
    db_id: str, keyword: str, table: str | None = None, column: str | None = None, limit: int = 10
) -> dict:
    """在文本列中模糊查找真实取值，用于确认 WHERE 条件里该写什么值。"""
    return tools.search_values(db_id, keyword, table, column, limit, ROLE)


@server.tool()
def run_sql(db_id: str, sql: str, max_rows: int = 50) -> dict:
    """只读执行单条 SELECT，返回列名和行（最多 max_rows 行）；出错时返回 error 和 message。"""
    return tools.run_sql(db_id, sql, max_rows, ROLE)


if __name__ == "__main__":
    server.run("stdio")
