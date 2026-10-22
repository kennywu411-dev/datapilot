"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

const LINKS = [
  { href: "/", label: "问数" },
  { href: "/eval", label: "评测看板" },
];

export function TopNav() {
  const path = usePathname();
  return (
    <nav className="inline-flex rounded-lg border p-0.5 text-sm">
      {LINKS.map((l) => (
        <Link
          key={l.href}
          href={l.href}
          className={cn(
            "rounded-md px-3 py-1 transition-colors",
            path === l.href ? "bg-accent text-foreground" : "text-muted-foreground hover:text-foreground",
          )}
        >
          {l.label}
        </Link>
      ))}
    </nav>
  );
}
