import type { Finding, PaperEvidence } from "@/lib/types";

const STATUS: Record<Finding["status"], { label: string; tone: string }> = {
  matched: { label: "找到实现", tone: "ok" },
  partial: { label: "部分匹配", tone: "warn" },
  not_found: { label: "未找到实现", tone: "muted" },
};

export function FindingStatus({ status, plain = false }: { status: Finding["status"]; plain?: boolean }) {
  return <span data-tone={STATUS[status].tone} className={plain ? "status-label" : "badge"}>{STATUS[status].label}</span>;
}

export function PaperEvidenceList({ evidence, onOpenPaper }: {
  evidence: PaperEvidence[];
  onOpenPaper: (page: number, quote: string) => void;
}) {
  return (
    <div className="space-y-3">
      {evidence.length === 0 && <p className="text-sm text-muted">这条目标暂未附论文引文。</p>}
      {evidence.map((item, index) => (
        <figure key={index} className="paper-evidence rounded-lg bg-subtle px-4 py-3">
          <figcaption className="mb-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
            <span className="font-medium text-muted">第 {item.page} 页</span>
            {item.verified === true && item.quote_match !== "partial" && <span data-tone="ok" className="status-label">引文已核验</span>}
            {item.verified === true && item.quote_match === "partial" && <span data-tone="warn" className="status-label" title="仅引文开头逐字匹配，后续内容未完整核验">引文部分匹配</span>}
            {item.verified === false && <span data-tone="fail" className="status-label">引文未通过核验</span>}
            {item.verified == null && <span data-tone="muted" className="status-label">未核验</span>}
            <button type="button" onClick={() => onOpenPaper(item.page, item.quote)} className="quiet-button -my-1 ml-auto">查看原文 ↗</button>
          </figcaption>
          <blockquote className="whitespace-pre-wrap text-sm leading-7 text-foreground/85">{item.quote}</blockquote>
        </figure>
      ))}
    </div>
  );
}
