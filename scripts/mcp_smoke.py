"""按 .mcp.json 里写的命令拉起 MCP Server，走一遍 MCP 客户端会做的事：列工具、调工具。

用来证明"能被真实的 Agent 产品调用"的配置本身是对的（客户端启动 MCP Server 的方式相同）。
用法：uv run python scripts/mcp_smoke.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    spec = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["datapilot"]
    params = StdioServerParameters(command=spec["command"], args=spec["args"], cwd=str(ROOT))
    async with Client(params) as client:
        tools = (await client.list_tools()).tools
        print("工具：", ", ".join(t.name for t in tools))
        calls = [
            ("list_tables", {"db_id": "superhero"}),
            ("search_values", {"db_id": "superhero", "keyword": "Marvel", "limit": 3}),
            (
                "run_sql",
                {
                    "db_id": "superhero",
                    "sql": "SELECT p.publisher_name, COUNT(*) AS n FROM superhero s "
                    "JOIN publisher p ON s.publisher_id = p.id "
                    "GROUP BY p.id ORDER BY n DESC LIMIT 3",
                },
            ),
            ("run_sql", {"db_id": "superhero", "sql": "DROP TABLE superhero"}),
        ]
        for name, args in calls:
            res = await client.call_tool(name, args)
            text = res.structured_content or "".join(getattr(c, "text", "") for c in res.content)
            print(f"\n> {name}({json.dumps(args, ensure_ascii=False)[:90]})")
            print(
                json.dumps(text, ensure_ascii=False)[:300]
                if not isinstance(text, str)
                else text[:300]
            )


if __name__ == "__main__":
    asyncio.run(main())
