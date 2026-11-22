"use client";

import type { RunEvent, VerificationSummary } from "@/lib/types";
import Markdown from "@/components/Markdown";

interface Props {
  events: RunEvent[];
  verification: VerificationSummary | null;
  missingIds: string[];
  filesTotal: number | null;
}

interface Stat {
  label: string;
  value: string;
  hint?: string;
  warn?: boolean;
}

/**
 * 覆盖率与预算。
 *
 * 为什么值得单独做一块：这个系统的产出"看起来对"很容易，用户需要能看到
 * **它到底看了多少、花了多少、什么时候停的**，才能判断这份解读可信到什么程度。
 * "我读了 3 个文件中的 2 个"和"我读了 312 个文件中的 40 个"，可信度完全不同。
 */
export default function CoverageCard({ events, verification, missingIds, filesTotal }: Props) {
  const runEnd = [...events].reverse().find((event) => event.type === "run_end")?.data;
  if (!runEnd) {
    return (
      <section className="rounded-lg border border-dashed border-line-strong p-4 text-sm text-muted">
        <h2 className="text-sm font-semibold text-foreground">覆盖率与预算</h2>
        <p className="mt-1 text-xs">
          跑完一次分析后，这里会显示：读了论文哪几页、仓库里读了多少文件、工具调用次数与 token 用量、
          <strong>以及引用核验率</strong>——产出可信到什么程度，靠这些数字判断。
        </p>
      </section>
    );
  }

  const usage = runEnd.usage ?? {};
  const limits = usage.limits ?? {};

  const pagesRead = new Set<number>();
  let readAllPages = false;
  const filesRead = new Set<string>();
  for (const event of events) {
    if (event.type !== "tool_call") continue;
    if (event.data.tool === "get_page_text" && typeof event.data.args?.page === "number") {
      pagesRead.add(event.data.args.page);
    }
    if (event.data.tool === "read_paper_all") readAllPages = true;
    if (event.data.tool === "read_file" && typeof event.data.args?.path === "string") {
      filesRead.add(event.data.args.path);
    }
  }

  const stats: Stat[] = [
    {
      label: "论文阅读",
      value: readAllPages ? "全文" : `${pagesRead.size} 页`,
      hint: readAllPages ? "一次读完" : pagesRead.size ? `第 ${[...pagesRead].sort((a, b) => a - b).join(", ")} 页` : undefined,
    },
    {
      label: "代码阅读",
      value: `${filesRead.size} 个文件`,
      hint: filesTotal ? `仓库共 ${filesTotal} 个文件（含依赖与二进制）` : undefined,
    },
    {
      label: "工具调用",
      value: `${usage.tool_calls ?? 0} / ${limits.max_tool_calls ?? "?"}`,
      warn: limits.max_tool_calls ? (usage.tool_calls ?? 0) >= limits.max_tool_calls : false,
    },
    {
      label: "输入 token（估算）",
      value: `${((usage.input_tokens ?? 0) / 1000).toFixed(1)}k / ${((limits.max_input_tokens ?? 0) / 1000).toFixed(0)}k`,
    },
    { label: "用时", value: `${usage.seconds ?? 0}s / ${limits.wall_clock_seconds ?? "?"}s` },
    {
      label: "引用核验",
      value: verification ? `${(verification.citation_verifiable_rate * 100).toFixed(0)}%` : "—",
      hint: verification ? `${verification.citations_verified}/${verification.citations_total} 条` : undefined,
      warn: verification ? verification.citations_failed > 0 : false,
    },
  ];

  return (
    <section className="px-3 py-3">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">覆盖率与预算</h2>
        <span data-tone={runEnd.status === "ok" ? "ok" : runEnd.status === "failed" ? "fail" : "warn"} className="badge">
          {runEnd.status} · {runEnd.stopped_reason}
        </span>
        {missingIds.length > 0 && (
          <span data-tone="warn" className="badge">
            未覆盖：{missingIds.join(", ")}
          </span>
        )}
      </div>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 lg:grid-cols-6">
        {stats.map((stat) => (
          <div key={stat.label} className="min-w-0">
            <dt className="text-xs text-muted">{stat.label}</dt>
            <dd className={`mt-0.5 text-sm font-medium tabular-nums ${stat.warn ? "text-warn" : ""}`}>{stat.value}</dd>
            {stat.hint && <dd className="mt-0.5 text-[11px] leading-snug text-muted">{stat.hint}</dd>}
          </div>
        ))}
      </dl>

      {runEnd.coverage_note && (
        <p className="mt-3 text-xs leading-5 text-muted">
          Agent 自述的覆盖情况：<Markdown inline text={runEnd.coverage_note} />
        </p>
      )}
    </section>
  );
}
