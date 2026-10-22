// 后端接口与 SSE 流式客户端。
// EventSource 只支持 GET，这里用 fetch + ReadableStream 自己解析 SSE，便于 POST 问题。

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
const IDLE_TIMEOUT_MS = 60_000; // 后端每 15 秒发心跳，60 秒没任何数据视为断线

export type Column = {
  name: string;
  type: string;
  primary_key: boolean;
  description: string;
  value_description: string;
};
export type TableInfo = {
  table: string;
  columns: Column[];
  foreign_keys: { column: string; ref_table: string; ref_column: string }[];
  sample_rows: unknown[][];
};
export type ChartSpec = { type: "kpi" | "line" | "bar" | "pie" | "table"; x?: string; y?: string[] };
export type TableData = { columns: string[]; rows: unknown[][]; truncated: boolean };
export type Step = { name: string; label: string; sql?: string; issues?: string[] };
export type Done = { cached: boolean; latency_s: number; cost_usd: number; model: string };

export type AskEvent =
  | { event: "step"; data: Step }
  | { event: "sql"; data: { sql: string } }
  | { event: "table"; data: TableData }
  | { event: "chart"; data: ChartSpec }
  | { event: "answer"; data: { text: string } }
  | { event: "error"; data: { message: string; sql?: string } }
  | { event: "done"; data: Done };

async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(`${API_BASE}${path}`);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export const api = {
  health: () => getJSON<{ store: string; model: string }>("/api/health"),
  databases: () => getJSON<{ db_id: string; tables: number }[]>("/api/databases"),
  schema: (db: string) => getJSON<{ db_id: string; tables: TableInfo[] }>(`/api/databases/${db}/schema`),
  examples: (db: string) => getJSON<string[]>(`/api/databases/${db}/examples`),
};

/** 发起一次问数，逐个回调事件。返回 true 表示收到 done，false 表示中途断线。 */
export async function ask(
  body: { session_id: string; db_id: string; question: string },
  onEvent: (e: AskEvent) => void,
  signal?: AbortSignal,
): Promise<boolean> {
  const r = await fetch(`${API_BASE}/api/ask`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!r.ok || !r.body) throw new Error(`请求失败：${r.status}`);

  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let gotDone = false;

  while (true) {
    // 每次读之前挂一个空闲计时器，读到数据就清掉；否则每个数据块都会留下一个 60 秒后
    // 无人处理的 reject（控制台 Uncaught in promise），计时器也会堆积
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<"idle">((resolve) => {
      timer = setTimeout(() => resolve("idle"), IDLE_TIMEOUT_MS);
    });
    const chunk = await Promise.race([reader.read(), timeout]);
    clearTimeout(timer);
    if (chunk === "idle") {
      reader.cancel().catch(() => {});
      return false;
    }
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    // SSE 以空行分隔事件；sse-starlette 用 \r\n
    const parts = buffer.split(/\r?\n\r?\n/);
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      let event = "";
      const data: string[] = [];
      for (const line of part.split(/\r?\n/)) {
        if (line.startsWith(":")) continue; // 心跳注释行
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (!event) continue;
      let payload: unknown;
      try {
        payload = JSON.parse(data.join("\n"));
      } catch {
        // 单条事件格式异常时如实报告，而不是让整个请求被当成"无法连接后端"
        onEvent({ event: "error", data: { message: `收到无法解析的 ${event} 事件` } });
        continue;
      }
      const parsed = { event, data: payload } as AskEvent;
      if (parsed.event === "done") gotDone = true;
      onEvent(parsed);
    }
  }
  return gotDone;
}
