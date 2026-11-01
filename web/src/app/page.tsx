"use client";

import { ArrowUp, NotePencil } from "@phosphor-icons/react";
import { useCallback, useEffect, useRef, useState } from "react";

import { Sidebar } from "@/components/sidebar";
import { TopNav } from "@/components/top-nav";
import { type Turn, TurnView } from "@/components/turn-view";
import { TooltipProvider } from "@/components/ui/tooltip";
import { api, ask } from "@/lib/api";
import { cn } from "@/lib/utils";

const newId = () => (typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : String(Date.now()));

export default function Home() {
  const [dbs, setDbs] = useState<{ db_id: string; tables: number }[]>([]);
  const [db, setDb] = useState("");
  const [roles, setRoles] = useState<{ role: string; label: string }[]>([]);
  const [role, setRole] = useState("");
  const [health, setHealth] = useState<{ store: string; model: string } | null>(null);
  const [backendDown, setBackendDown] = useState(false);
  const [examples, setExamples] = useState<string[]>([]);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [session, setSession] = useState(newId);
  const bottom = useRef<HTMLDivElement>(null);
  const running = turns.some((t) => t.status === "running");

  useEffect(() => {
    Promise.all([api.databases(), api.health()])
      .then(([d, h]) => {
        setDbs(d);
        setHealth(h);
        setDb((cur) => cur || (d.find((x) => x.db_id === "superhero") ?? d[0])?.db_id || "");
      })
      .catch(() => setBackendDown(true));
  }, []);

  useEffect(() => {
    if (db) api.examples(db).then(setExamples).catch(() => setExamples([]));
    if (!db) return;
    api
      .roles(db)
      .then((r) => {
        setRoles(r);
        setRole(r[0]?.role ?? "");
      })
      .catch(() => {
        setRoles([]);
        setRole("");
      });
  }, [db]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns]);

  const patch = (id: string, f: (t: Turn) => Turn) => setTurns((ts) => ts.map((t) => (t.id === id ? f(t) : t)));

  const run = useCallback(
    async (question: string, existingId?: string) => {
      const id = existingId ?? newId();
      const fresh: Turn = { id, question, status: "running", steps: [] };
      setTurns((ts) => (existingId ? ts.map((t) => (t.id === id ? fresh : t)) : [...ts, fresh]));
      try {
        const complete = await ask({ session_id: session, db_id: db, question, role: role || undefined }, (e) => {
          patch(id, (t) => {
            switch (e.event) {
              case "step":
                return { ...t, steps: [...t.steps, e.data] };
              case "sql":
                return { ...t, sql: e.data.sql };
              case "table":
                return { ...t, table: e.data };
              case "chart":
                return { ...t, chart: e.data };
              case "answer":
                return { ...t, answer: e.data.text };
              case "error":
                return { ...t, status: "error", error: e.data.message, sql: e.data.sql ?? t.sql };
              case "done":
                return { ...t, done: e.data, status: t.status === "error" ? "error" : "done" };
            }
          });
        });
        if (!complete) patch(id, (t) => ({ ...t, status: "disconnected" }));
      } catch (err) {
        // 后端明确拒绝（限流等）时原样展示，其余才是连不上
        const e = err as Error;
        const message = e.name === "HttpError" ? e.message : `无法连接后端：${e.message}`;
        patch(id, (t) => ({ ...t, status: "error", error: message }));
      }
    },
    [db, session, role],
  );

  const submit = () => {
    const q = input.trim();
    if (!q || running || !db) return;
    setInput("");
    run(q);
  };

  const resetConversation = () => {
    setTurns([]);
    setSession(newId());
  };

  return (
    <TooltipProvider delay={200}>
      <div className="flex h-[100dvh] overflow-hidden">
        <div className="hidden md:block">
          <Sidebar
            dbs={dbs}
            db={db}
            health={health}
            onDbChange={(d) => {
              setDb(d);
              resetConversation();
            }}
            roles={roles}
            role={role}
            onRoleChange={(r) => {
              setRole(r);
              resetConversation();
            }}
          />
        </div>

        <main className="flex min-w-0 flex-1 flex-col">
          <header className="flex h-14 shrink-0 items-center justify-between border-b px-4 md:px-8">
            <select
              aria-label="数据库"
              value={db}
              onChange={(e) => {
                setDb(e.target.value);
                resetConversation();
              }}
              className="h-8 rounded-md border bg-background px-2 font-mono text-sm md:hidden"
            >
              {dbs.map((d) => (
                <option key={d.db_id} value={d.db_id}>
                  {d.db_id}
                </option>
              ))}
            </select>
            <span className="hidden text-sm text-muted-foreground md:inline">
              {db ? (
                <>
                  正在查询 <span className="font-mono text-foreground">{db}</span>
                </>
              ) : (
                "选择一个数据库开始"
              )}
            </span>
            <div className="flex items-center gap-2">
            <TopNav />
            <button
              onClick={resetConversation}
              disabled={running || turns.length === 0}
              className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-sm text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-40"
            >
              <NotePencil size={15} />
              新对话
            </button>
            </div>
          </header>

          <div className="min-h-0 flex-1 overflow-y-auto">
            <div className="mx-auto w-full max-w-3xl space-y-10 px-4 py-8 md:px-8">
              {backendDown && (
                <p className="rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm">
                  连不上后端。请先运行 <code className="font-mono">uv run uvicorn datapilot.api:app --port 8000</code>，再刷新页面。
                </p>
              )}

              {turns.length === 0 && !backendDown && (
                <section className="pt-[12vh]">
                  <h1 className="text-2xl font-medium tracking-tight">用中文问数据</h1>
                  <p className="mt-2 max-w-[56ch] text-sm leading-relaxed text-muted-foreground">
                    系统会读表结构、写 SQL、在只读库上执行，出错时自动修正，最后给出答案、表格和图表。可以接着追问。
                  </p>
                  {examples.length > 0 && (
                    <div className="mt-8 grid gap-2 sm:grid-cols-2">
                      {examples.map((q) => (
                        <button
                          key={q}
                          onClick={() => run(q)}
                          className="rounded-lg border px-3.5 py-3 text-left text-sm leading-snug text-foreground/85 transition-colors hover:border-primary/40 hover:bg-accent/50 active:scale-[0.99]"
                        >
                          {q}
                        </button>
                      ))}
                    </div>
                  )}
                </section>
              )}

              {turns.map((t) => (
                <TurnView key={t.id} turn={t} onRetry={() => run(t.question, t.id)} />
              ))}
              <div ref={bottom} />
            </div>
          </div>

          <div className="shrink-0 px-4 pb-5 md:px-8">
            <form
              onSubmit={(e) => {
                e.preventDefault();
                submit();
              }}
              className="mx-auto flex max-w-3xl items-end gap-2 rounded-xl border bg-surface px-3 py-2 focus-within:border-primary/40"
            >
              <label htmlFor="q" className="sr-only">
                问题
              </label>
              <textarea
                id="q"
                rows={1}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    submit();
                  }
                }}
                placeholder={turns.length ? "接着追问，比如：那按年份分开呢？" : "问一个关于这个数据库的问题"}
                className="max-h-40 min-h-[2.25rem] flex-1 resize-none bg-transparent py-1.5 text-sm outline-none placeholder:text-muted-foreground/80"
              />
              <button
                type="submit"
                disabled={!input.trim() || running || !db}
                aria-label="发送"
                className={cn(
                  "mb-0.5 inline-flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground transition-transform active:scale-95",
                  "disabled:bg-muted disabled:text-muted-foreground",
                )}
              >
                <ArrowUp size={16} weight="bold" />
              </button>
            </form>
          </div>
        </main>
      </div>
    </TooltipProvider>
  );
}
