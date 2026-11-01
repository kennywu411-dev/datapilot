"use client";

import { Check, Copy } from "@phosphor-icons/react";
import { Highlight, themes } from "prism-react-renderer";
import { useState } from "react";

import { useIsDark } from "@/lib/theme";

/** 把一行 SQL 在主要子句前断行，便于阅读 */
function pretty(sql: string) {
  return sql
    .replace(/\s+/g, " ")
    .replace(/\s+(FROM|WHERE|GROUP BY|HAVING|ORDER BY|LIMIT|UNION|(?:LEFT |INNER |RIGHT )?JOIN)\s+/gi, "\n$1 ")
    .trim();
}

export function SqlBlock({ sql }: { sql: string }) {
  const [copied, setCopied] = useState(false);
  const dark = useIsDark();
  const code = pretty(sql);

  return (
    <div className="group relative rounded-lg border bg-code">
      <button
        aria-label="复制 SQL"
        onClick={() => {
          navigator.clipboard.writeText(sql);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        }}
        className="absolute right-2 top-2 rounded-md p-1.5 text-muted-foreground opacity-0 transition-opacity hover:bg-accent hover:text-foreground focus:opacity-100 group-hover:opacity-100"
      >
        {copied ? <Check size={14} /> : <Copy size={14} />}
      </button>
      <Highlight code={code} language="sql" theme={dark ? themes.vsDark : themes.vsLight}>
        {({ tokens, getLineProps, getTokenProps }) => (
          <pre className="overflow-x-auto px-4 py-3 font-mono text-[13px] leading-relaxed" style={{ background: "transparent" }}>
            {tokens.map((line, i) => (
              <div key={i} {...getLineProps({ line })}>
                {line.map((token, k) => (
                  <span key={k} {...getTokenProps({ token })} />
                ))}
              </div>
            ))}
          </pre>
        )}
      </Highlight>
    </div>
  );
}
