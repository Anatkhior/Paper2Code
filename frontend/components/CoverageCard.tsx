"use client";

import type { RunEvent, VerificationSummary } from "@/lib/types";

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
      <section className="rounded-lg border border-dashed border-neutral-300 p-4 text-sm text-neutral-500 dark:border-neutral-800">
        <h2 className="text-sm font-semibold text-neutral-700 dark:text-neutral-300">覆盖率与预算</h2>
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
    <section className="rounded-lg border border-neutral-200 bg-white p-3 dark:border-neutral-800 dark:bg-neutral-950">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">覆盖率与预算</h2>
        <span
          className={`rounded px-1.5 py-0.5 text-[11px] ${
            runEnd.status === "ok"
              ? "bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300"
              : "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-200"
          }`}
        >
          {runEnd.status} · {runEnd.stopped_reason}
        </span>
        {missingIds.length > 0 && (
          <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] text-amber-800 dark:bg-amber-950 dark:text-amber-200">
            未覆盖：{missingIds.join(", ")}
          </span>
        )}
      </div>

      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        {stats.map((stat) => (
          <div key={stat.label}>
            <dt className="text-[11px] text-neutral-500">{stat.label}</dt>
            <dd className={`font-mono text-sm ${stat.warn ? "text-amber-600" : ""}`}>{stat.value}</dd>
            {stat.hint && <dd className="mt-0.5 text-[10px] leading-tight text-neutral-400">{stat.hint}</dd>}
          </div>
        ))}
      </dl>

      {runEnd.coverage_note && (
        <p className="mt-2 text-[11px] text-neutral-500">
          Agent 自述的覆盖情况：{runEnd.coverage_note}
        </p>
      )}
    </section>
  );
}
