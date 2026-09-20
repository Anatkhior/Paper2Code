import type { Finding, PaperEvidence } from "@/lib/types";

const STATUS: Record<Finding["status"], { label: string; className: string }> = {
  matched: { label: "找到实现", className: "text-emerald-700 dark:text-emerald-400" },
  partial: { label: "部分匹配", className: "text-amber-700 dark:text-amber-400" },
  not_found: { label: "未找到实现", className: "text-neutral-500" },
};

export function FindingStatus({ status }: { status: Finding["status"] }) {
  return <span className={`text-xs ${STATUS[status].className}`}>{STATUS[status].label}</span>;
}

export function PaperEvidenceList({ evidence, onOpenPaper }: {
  evidence: PaperEvidence[];
  onOpenPaper: (page: number, quote: string) => void;
}) {
  return (
    <div className="space-y-3">
      {evidence.length === 0 && <p className="text-sm text-neutral-500">这条目标暂未附论文引文。</p>}
      {evidence.map((item, index) => (
        <div key={index} className="paper-evidence border-l-2 border-teal-500/50 pl-4">
          <div className="mb-1 flex flex-wrap items-center gap-2 text-xs">
            <span className="font-medium text-neutral-500">第 {item.page} 页</span>
            {item.verified === true && item.quote_match !== "partial" && <span className="text-emerald-700 dark:text-emerald-400">引文已核验</span>}
            {item.verified === true && item.quote_match === "partial" && <span className="text-amber-700 dark:text-amber-400" title="仅引文开头逐字匹配，后续内容未完整核验">引文部分匹配</span>}
            {item.verified === false && <span className="text-red-600">引文未通过核验</span>}
            {item.verified == null && <span className="text-neutral-500">未核验</span>}
            <button type="button" onClick={() => onOpenPaper(item.page, item.quote)} className="ml-auto text-teal-700 underline underline-offset-4 dark:text-teal-300">查看原文 ↗</button>
          </div>
          <blockquote className="whitespace-pre-wrap text-sm leading-7 text-neutral-600 dark:text-neutral-300">{item.quote}</blockquote>
        </div>
      ))}
    </div>
  );
}
