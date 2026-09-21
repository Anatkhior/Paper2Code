"use client";

import type { Finding } from "@/lib/types";
import Markdown from "@/components/Markdown";
import { FindingStatus, PaperEvidenceList } from "@/components/Evidence";

interface Props {
  finding: Finding | undefined;
  missing: boolean;
  busy: boolean;
  selected: boolean;
  onOpenPaper: (page: number, quote: string) => void;
  onOpenCode: (path: string, start: number, end: number, why: string) => void;
  onAsk: (id: string, name: string) => void;
}

function VerifyBadge({ state }: { state?: string }) {
  if (state === "verified") return <span className="text-xs text-emerald-700 dark:text-emerald-400">已核验</span>;
  if (state === "failed") return <span className="text-xs text-red-600">未通过核验</span>;
  return <span className="text-xs text-neutral-500">待核验</span>;
}

export default function ComparePanel({ finding, missing, busy, selected, onOpenPaper, onOpenCode, onAsk }: Props) {
  if (!finding) {
    return <p className={`mt-5 border-t border-neutral-200 pt-4 text-sm dark:border-neutral-800 ${missing ? "text-amber-700 dark:text-amber-400" : "text-neutral-500"}`}>
      {missing ? "本轮未产出这条结论。请查看行动轨迹中的结束原因；这不等于未找到实现。" : busy && selected ? "正在定位，结论会在这里逐步出现。" : selected ? "已选中这条创新点，开始定位后在这里阅读结果。" : "这条尚未选中。勾选后可定位对应实现。"}
    </p>;
  }

  const explanation = finding.explanation;
  return (
    <article className="finding-detail mt-5 space-y-6 border-t border-neutral-200 pt-4 dark:border-neutral-800">
      <header className="flex flex-wrap items-center gap-3">
        <h3 className="text-sm font-semibold">定位结论</h3>
        <FindingStatus status={finding.status} />
        <span className="text-xs text-neutral-500" title={finding.confidence_reason}>置信度 {(finding.confidence * 100).toFixed(0)}%</span>
        <button type="button" onClick={() => onAsk(finding.id, finding.name ?? finding.id)} className="ml-auto text-xs text-teal-700 underline underline-offset-4 dark:text-teal-300">就这条追问 ↗</button>
      </header>

      {explanation?.intuition && <section className="reading-section">
        <h4>它在做什么</h4>
        <Markdown text={explanation.intuition} />
        {explanation.math && <details className="mt-3">
          <summary className="text-xs text-neutral-500">公式 · 模型重构，非论文原文</summary>
          <code className="mt-2 block whitespace-pre-wrap rounded bg-neutral-100 p-3 text-xs dark:bg-neutral-900">{explanation.math}</code>
        </details>}
      </section>}

      <section className="reading-section">
        <h4>论文证据</h4>
        <PaperEvidenceList evidence={finding.paper_evidence} onOpenPaper={onOpenPaper} />
      </section>

      <section className="reading-section">
        <h4>代码实现</h4>
        {finding.status === "not_found" ? (
          <div className="rounded-lg bg-neutral-100 p-4 dark:bg-neutral-900">
            <Markdown text={finding.not_found_reason ?? ""} />
            {finding.searched.length > 0 && <p className="mt-3 text-xs text-neutral-500">已搜索：{finding.searched.join(" · ")}</p>}
          </div>
        ) : (
          <div className="code-evidence-list divide-y divide-neutral-200 dark:divide-neutral-800">
            {finding.code_evidence.map((evidence, index) => (
              <div key={`${evidence.path}-${index}`} className="py-3 first:pt-0">
                <div className="flex flex-wrap items-center gap-2">
                  <button type="button" onClick={() => onOpenCode(evidence.path, evidence.line_start, evidence.line_end, evidence.why)} className="text-left font-mono text-xs font-medium text-teal-700 underline underline-offset-4 dark:text-teal-300">
                    {evidence.path}:{evidence.line_start}-{evidence.line_end} ↗
                  </button>
                  <VerifyBadge state={evidence.verification?.state} />
                  {evidence.symbol && <span className="text-xs text-neutral-500">{evidence.symbol}</span>}
                </div>
                <div className="mt-1 text-sm leading-7 text-neutral-600 dark:text-neutral-300"><Markdown text={evidence.why ?? ""} /></div>
                {!!evidence.verification?.failures?.length && <ul className="mt-1 list-disc pl-4 text-xs text-red-600">
                  {evidence.verification.failures.map((failure) => <li key={failure}>{failure}</li>)}
                </ul>}
                {evidence.snippet_sha256 && <details className="mt-1 text-[11px] text-neutral-500"><summary>引用凭据</summary><code className="break-all">片段哈希 {evidence.snippet_sha256}</code></details>}
              </div>
            ))}
          </div>
        )}
      </section>

      <details className="reading-disclosure">
        <summary>阅读提示与判断依据</summary>
        <div className="mt-4 space-y-4 text-sm leading-7">
          {!!explanation?.pitfalls?.length && <div><h5 className="font-medium">容易搞错的地方</h5><ul className="list-disc pl-5">{explanation.pitfalls.map((pitfall) => <li key={pitfall}>{pitfall}</li>)}</ul></div>}
          {!!explanation?.read_next?.length && <div><h5 className="font-medium">接下来读</h5><p className="font-mono text-xs">{explanation.read_next.join(" · ")}</p></div>}
          <p className="text-xs text-neutral-500">置信度理由：{finding.confidence_reason}</p>
        </div>
      </details>
    </article>
  );
}
