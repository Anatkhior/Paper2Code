"use client";

import type { Finding, VerificationSummary } from "@/lib/types";

interface Props {
  findings: Finding[];
  verification: VerificationSummary | null;
  missingIds: string[];
  commitSha: string | null;
  busy: boolean;
  /** 点论文证据：打开那一页原文 */
  onOpenPaper: (page: number, quote: string) => void;
  /** 点代码引用：打开那段代码 */
  onOpenCode: (path: string, start: number, end: number, why: string) => void;
  /** 就这一条继续追问 */
  onAsk?: (id: string, name: string) => void;
}

const STATUS: Record<Finding["status"], { label: string; className: string }> = {
  matched: { label: "找到实现", className: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-200" },
  partial: { label: "部分匹配", className: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200" },
  not_found: { label: "未找到实现", className: "bg-neutral-200 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300" },
};

function VerifyBadge({ state }: { state?: string }) {
  if (state === "verified") return <span className="text-[11px] text-green-600">✅ 已核验</span>;
  if (state === "failed") return <span className="text-[11px] text-red-600">⚠ 未通过核验</span>;
  return <span className="text-[11px] text-neutral-400">待核验</span>;
}

export default function ComparePanel({
  findings,
  verification,
  missingIds,
  commitSha,
  busy,
  onOpenPaper,
  onOpenCode,
  onAsk,
}: Props) {
  if (findings.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-neutral-300 p-6 text-sm text-neutral-500 dark:border-neutral-800">
        {busy ? "阶段 B 正在跑：Agent 在仓库里找实现。" : "阶段 B 完成后，这里会逐条给出「论文证据 ↔ 代码引用」的对照。"}
        <p className="mt-2 text-xs">
          左边是你点得开原文的论文证据，右边是点得开代码的引用。每条引用都由后端按 commit 从 git 对象里重放核验。
        </p>
        <p className="mt-2 text-xs">
          每条结论右上角还有「就这条追问」——解释看不明白时，可以在下面第 8 节让它继续讲，
          它会自己去翻代码和论文，回答里给出的代码位置也会被核对。
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {verification && (
        <div className="rounded-lg border border-neutral-200 bg-white p-3 text-sm dark:border-neutral-800 dark:bg-neutral-950">
          <p className="font-medium">
            引用核验率 {(verification.citation_verifiable_rate * 100).toFixed(0)}%
            <span className="ml-2 font-normal text-neutral-500">
              （{verification.citations_verified}/{verification.citations_total} 条通过机械核验）
            </span>
          </p>
          <p className="mt-1 font-mono text-[11px] text-neutral-500">
            commit {commitSha?.slice(0, 12) ?? verification.commit_sha.slice(0, 12)} ·{" "}
            {Object.entries(verification.status_counts)
              .map(([key, value]) => `${key}:${value}`)
              .join(" · ")}
          </p>
          {missingIds.length > 0 && (
            <p className="mt-1 text-[11px] text-amber-600">
              没有给出结论的创新点：{missingIds.join(", ")}（预算用尽或 Agent 提前结束）
            </p>
          )}
        </div>
      )}

      {findings.map((finding) => (
        <article
          key={finding.id}
          className="overflow-hidden rounded-lg border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-950"
        >
          <header className="flex flex-wrap items-center gap-2 border-b border-neutral-200 px-3 py-2 dark:border-neutral-800">
            <span className="font-medium">{finding.name ?? finding.id}</span>
            <span className={`rounded px-1.5 py-0.5 text-[11px] ${STATUS[finding.status].className}`}>
              {STATUS[finding.status].label}
            </span>
            <span className="text-[11px] text-neutral-500">置信度 {(finding.confidence * 100).toFixed(0)}%</span>
            <span className="ml-auto flex items-center gap-2">
              {onAsk && (
                <button
                  type="button"
                  onClick={() => onAsk(finding.id, finding.name ?? finding.id)}
                  className="rounded border border-neutral-300 px-2 py-0.5 text-[11px] hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
                >
                  就这条追问
                </button>
              )}
              <span className="text-[11px] text-neutral-400">{finding.id}</span>
            </span>
          </header>

          <div className="grid divide-y divide-neutral-200 md:grid-cols-2 md:divide-x md:divide-y-0 dark:divide-neutral-800">
            {/* 左：论文侧 */}
            <div className="p-3">
              <h4 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
                论文怎么说的
              </h4>
              {finding.paper_evidence.length === 0 && (
                <p className="text-xs text-neutral-500">这条创新点没有登记论文证据。</p>
              )}
              <div className="space-y-2">
                {finding.paper_evidence.map((evidence, index) => (
                  <button
                    key={index}
                    type="button"
                    onClick={() => onOpenPaper(evidence.page, evidence.quote)}
                    className="block w-full rounded border border-neutral-200 p-2 text-left transition hover:border-neutral-400 hover:bg-neutral-50 dark:border-neutral-800 dark:hover:border-neutral-600 dark:hover:bg-neutral-900"
                  >
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-[11px] text-neutral-500">第 {evidence.page} 页</span>
                      {evidence.verified === true && evidence.quote_match !== "partial" && (
                        <span className="text-[11px] text-green-600">✅ 引文已核验</span>
                      )}
                      {evidence.verified === true && evidence.quote_match === "partial" && (
                        <span
                          className="text-[11px] text-amber-600"
                          title="只有开头部分逐字匹配到了这一页，后面的内容没有逐字出现——警惕后半段是编的"
                        >
                          ◐ 引文部分匹配
                        </span>
                      )}
                      {evidence.verified === false && <span className="text-[11px] text-red-600">⚠ 引文未通过核验</span>}
                      <span className="ml-auto text-[11px] text-neutral-400">点开看原文 ↗</span>
                    </div>
                    <p className="mt-1 whitespace-pre-wrap text-xs text-neutral-700 dark:text-neutral-300">
                      “{evidence.quote}”
                    </p>
                  </button>
                ))}
              </div>

              {finding.explanation?.intuition && (
                <div className="mt-3">
                  <h5 className="mb-1 text-[11px] font-semibold text-neutral-500">它在做什么</h5>
                  <p className="text-xs leading-relaxed text-neutral-700 dark:text-neutral-300">
                    {finding.explanation.intuition}
                  </p>
                </div>
              )}
              {finding.explanation?.math && (
                <p className="mt-1 text-xs">
                  <span className="text-neutral-500">公式（模型重构，非论文原文）：</span>
                  <code className="ml-1 font-mono">{finding.explanation.math}</code>
                </p>
              )}
            </div>

            {/* 右：代码侧 */}
            <div className="p-3">
              <h4 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-neutral-500">
                代码怎么写的
              </h4>

              {finding.status === "not_found" ? (
                <div className="rounded border border-neutral-200 p-2 text-xs dark:border-neutral-800">
                  <p className="text-neutral-700 dark:text-neutral-300">
                    <span className="text-neutral-500">为什么判断没有实现：</span>
                    {finding.not_found_reason}
                  </p>
                  {finding.searched.length > 0 && (
                    <div className="mt-2 flex flex-wrap items-center gap-1">
                      <span className="text-neutral-500">搜过：</span>
                      {finding.searched.map((item) => (
                        <span
                          key={item}
                          className="rounded bg-neutral-100 px-1.5 py-0.5 font-mono text-[11px] dark:bg-neutral-800"
                        >
                          {item}
                        </span>
                      ))}
                    </div>
                  )}
                  <p className="mt-2 text-[11px] text-neutral-500">
                    找不到实现不是失败——编一个看起来合理的实现才是。
                  </p>
                </div>
              ) : (
                <div className="space-y-2">
                  {finding.code_evidence.map((evidence, index) => (
                    <button
                      key={`${evidence.path}-${index}`}
                      type="button"
                      onClick={() =>
                        onOpenCode(evidence.path, evidence.line_start, evidence.line_end, evidence.why)
                      }
                      className="block w-full rounded border border-neutral-200 p-2 text-left transition hover:border-neutral-400 hover:bg-neutral-50 dark:border-neutral-800 dark:hover:border-neutral-600 dark:hover:bg-neutral-900"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <code className="font-mono text-[12px] font-medium">
                          {evidence.path}:{evidence.line_start}-{evidence.line_end}
                        </code>
                        {evidence.symbol && (
                          <span className="text-[11px] text-neutral-500">{evidence.symbol}</span>
                        )}
                        <VerifyBadge state={evidence.verification?.state} />
                        <span className="ml-auto text-[11px] text-neutral-400">点开看代码 ↗</span>
                      </div>
                      <p className="mt-1 text-xs text-neutral-700 dark:text-neutral-300">{evidence.why}</p>
                      {evidence.verification?.failures?.length ? (
                        <ul className="mt-1 list-disc pl-4 text-[11px] text-red-600">
                          {evidence.verification.failures.map((failure) => (
                            <li key={failure}>{failure}</li>
                          ))}
                        </ul>
                      ) : null}
                      {evidence.snippet_sha256 && (
                        <p className="mt-1 font-mono text-[10px] text-neutral-400">
                          片段哈希 {evidence.snippet_sha256.slice(0, 16)}…
                        </p>
                      )}
                    </button>
                  ))}
                </div>
              )}

              {finding.explanation?.code_walkthrough?.length ? (
                <div className="mt-3">
                  <h5 className="mb-1 text-[11px] font-semibold text-neutral-500">逐段讲解</h5>
                  <ol className="space-y-1">
                    {finding.explanation.code_walkthrough.map((step, stepIndex) => (
                      <li key={stepIndex} className="rounded border border-neutral-200 p-2 text-xs dark:border-neutral-800">
                        <code className="font-mono text-[11px] text-neutral-500">{step.line_ref}</code>
                        <p className="mt-0.5 text-neutral-700 dark:text-neutral-300">{step.text}</p>
                      </li>
                    ))}
                  </ol>
                </div>
              ) : null}
            </div>
          </div>

          {/* 全宽底栏：常见误解 + 接下来读什么 + 置信度理由 */}
          <footer className="space-y-2 border-t border-neutral-200 px-3 py-2 dark:border-neutral-800">
            {finding.explanation?.pitfalls?.length ? (
              <div>
                <h5 className="text-[11px] font-semibold text-neutral-500">容易搞错的地方</h5>
                <ul className="mt-0.5 list-disc space-y-0.5 pl-4 text-xs text-neutral-700 dark:text-neutral-300">
                  {finding.explanation.pitfalls.map((pitfall) => (
                    <li key={pitfall}>{pitfall}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {finding.explanation?.read_next?.length ? (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[11px] font-semibold text-neutral-500">接下来读</span>
                {finding.explanation.read_next.map((item) => (
                  <span key={item} className="rounded bg-neutral-100 px-1.5 py-0.5 font-mono text-[11px] dark:bg-neutral-800">
                    {item}
                  </span>
                ))}
              </div>
            ) : null}
            <p className="text-[11px] text-neutral-500">置信度理由：{finding.confidence_reason}</p>
          </footer>
        </article>
      ))}
    </div>
  );
}
