"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export interface CodePane {
  path: string;
  start: number;
  end: number;
  why?: string;
  lines: { n: number; text: string }[];
  total?: number;
  commit?: string;
  sourceUrl?: string | null;
  loading: boolean;
  error?: string | null;
}

export interface PaperPane {
  page: number;
  quote?: string;
  text?: string;
  pageCount?: number;
  loading: boolean;
  error?: string | null;
}

type PaperMode = "pdf" | "text";

interface Props {
  left: PaperPane | null;
  right: CodePane | null;
  /** 论文 PDF 的直链（用浏览器自带的阅读器渲染真实排版） */
  pdfUrl?: string | null;
  onGoToPage: (page: number) => void;
  /** 用户划选原文后，把这一段作为定位目标加进清单 */
  onSelectTarget?: (quote: string, page: number) => void;
}

/** 在原文里定位引文，忽略空白差异；找不到就返回 null（不硬套）。 */
function locateQuote(text: string, quote?: string) {
  if (!quote || !quote.trim()) return null;
  const direct = text.indexOf(quote);
  if (direct >= 0) {
    return { before: text.slice(0, direct), match: quote, after: text.slice(direct + quote.length) };
  }
  const squash = (value: string) => value.replace(/\s+/g, "");
  const flatText = squash(text);
  const flatQuote = squash(quote);
  const at = flatText.indexOf(flatQuote);
  if (at < 0) return null;
  // 建立「压缩后下标 → 原文下标」的映射，才能把高亮落回原文
  const map: number[] = [];
  for (let index = 0; index < text.length; index += 1) {
    if (!/\s/.test(text[index])) map.push(index);
  }
  const start = map[at];
  const end = map[Math.min(at + flatQuote.length - 1, map.length - 1)];
  if (start === undefined || end === undefined) return null;
  return { before: text.slice(0, start), match: text.slice(start, end + 1), after: text.slice(end + 1) };
}

/**
 * 双栏对照阅读器：左边论文原文，右边代码实现。
 *
 * 两条设计原则：
 * 1. **两边都要尽可能大**：占满可用的高度，各自独立滚动（滚代码不会把原文滚走）。
 * 2. 显示的内容与**被核验的内容是同一份**：代码由后端按 commit 从 git 对象里读出来。
 */
/**
 * 把引文压成适合塞进浏览器 PDF 阅读器 `#search=` 的短词。
 * Chromium / Firefox 的内置阅读器都支持 `#search=`（会在页面里高亮命中），
 * 但太长或带换行的串会匹配不上，所以只取前几个词。
 */
function pdfSearchTerm(quote?: string) {
  if (!quote) return "";
  const cleaned = quote
    .replace(/[\u201c\u201d"'`]/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!cleaned) return "";
  return cleaned.split(" ").slice(0, 8).join(" ").slice(0, 60);
}


export default function Reader({ left, right, pdfUrl, onGoToPage, onSelectTarget }: Props) {
  const [paperMode, setPaperMode] = useState<PaperMode>("pdf");
  const focusRef = useRef<HTMLDivElement>(null);
  const paperMarkRef = useRef<HTMLElement>(null);
  const paperBodyRef = useRef<HTMLDivElement>(null);
  const [selection, setSelection] = useState<{ text: string; x: number; y: number } | null>(null);
  const quoteParts = useMemo(
    () => (left?.text ? locateQuote(left.text, left.quote) : null),
    [left?.text, left?.quote],
  );

  useEffect(() => {
    focusRef.current?.scrollIntoView({ block: "center" });
  }, [right?.path, right?.start, right?.end, right?.lines]);

  /**
   * 左栏的高亮自动进视野。
   *
   * 用户不用自己滚：点了「查看原文」→ 页面滚到阅读器 → 这一页的文本可能刚好加载完，
   * 高亮又落在滚动区外面。这里在引文/该页文本/视图模式变化时把 <mark> 挪到可视区中间。
   */
  useEffect(() => {
    if (!quoteParts) return;
    const timer = window.setTimeout(() => {
      paperMarkRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
    }, 60);
    return () => window.clearTimeout(timer);
  }, [left?.quote, left?.text, left?.page, paperMode, quoteParts]);

  /**
   * 划选一段原文 → 弹出「以此为目标」。
   * 只认**左栏里**的选中内容：右栏是代码，划选它不该触发这个动作。
   */
  const handleMouseUp = useCallback(() => {
    const active = window.getSelection();
    const text = active?.toString().trim() ?? "";
    const anchor = active?.anchorNode ?? null;
    if (!text || text.length < 8 || !paperBodyRef.current || !anchor) {
      setSelection(null);
      return;
    }
    if (!paperBodyRef.current.contains(anchor)) {
      setSelection(null);
      return;
    }
    const rect = active && active.rangeCount > 0 ? active.getRangeAt(0).getBoundingClientRect() : null;
    setSelection({
      text: text.slice(0, 1200),
      x: rect ? Math.min(Math.max(rect.left + rect.width / 2, 90), window.innerWidth - 90) : 180,
      y: rect ? rect.top : 120,
    });
  }, []);

  // PDF 视图的高亮：浏览器内置阅读器不允许外部脚本操作 DOM，但支持用 `#search=` 触发
  // 它自己的查找并高亮命中——这是"在 PDF 原版里也高亮同一段"的现实做法。
  const pdfSearch = useMemo(() => pdfSearchTerm(left?.quote), [left?.quote]);
  const pdfSrc = pdfUrl
    ? `${pdfUrl}#page=${left?.page ?? 1}&view=FitH&toolbar=1${pdfSearch ? `&search=${encodeURIComponent(pdfSearch)}` : ""}`
    : null;

  return (
    <div className="relative">
      <div className="mb-2 flex flex-wrap items-center gap-2 rounded border border-dashed border-neutral-300 px-3 py-1.5 text-[11px] text-neutral-500 dark:border-neutral-800">
        <span>左栏可以切两种看法：</span>
        <button
          type="button"
          onClick={() => setPaperMode("pdf")}
          className={`rounded px-2 py-0.5 ${paperMode === "pdf" ? "bg-neutral-900 text-white dark:bg-neutral-100 dark:text-neutral-900" : "border border-neutral-300 dark:border-neutral-700"}`}
        >
          PDF 原版（真实排版，滚动看全文）
        </button>
        <button
          type="button"
          onClick={() => setPaperMode("text")}
          className={`rounded px-2 py-0.5 ${paperMode === "text" ? "bg-neutral-900 text-white dark:bg-neutral-100 dark:text-neutral-900" : "border border-neutral-300 dark:border-neutral-700"}`}
        >
          原文文本（可划选加目标）
        </button>
        <span>
          {paperMode === "pdf"
            ? "PDF 视图是真实排版（公式、图都在），但里面选中的文字拿不到。要划选一段原文加成目标，请切到「原文文本」，划选后点「以此为目标定位代码」。"
            : "在文本视图里用鼠标划选一段原文 → 点「以此为目标定位代码」，系统就去找这段话对应的实现。"}
        </span>
      </div>
      <div className="grid gap-3 lg:grid-cols-2" onMouseUp={handleMouseUp}>
      {selection && onSelectTarget && (
        <button
          type="button"
          style={{
            position: "fixed",
            left: selection.x,
            top: Math.max(8, selection.y - 44),
            transform: "translateX(-50%)",
          }}
          className="z-40 rounded-md bg-neutral-900 px-3 py-1.5 text-xs font-medium text-white shadow-lg dark:bg-neutral-100 dark:text-neutral-900"
          onClick={() => {
            onSelectTarget(selection.text, left?.page ?? 1);
            setSelection(null);
            window.getSelection()?.removeAllRanges();
          }}
        >
          以此为目标定位代码
        </button>
      )}
      {/* ---------------- 左：论文原文 ---------------- */}
      <section className="flex h-[62vh] min-h-[380px] flex-col overflow-hidden rounded-lg border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-950">
        <header className="flex flex-wrap items-center gap-2 border-b border-neutral-200 px-3 py-2 dark:border-neutral-800">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-neutral-500">论文原文</h3>
          {left ? (
            <>
              <span className="font-mono text-[11px] text-neutral-500">
                第 {left.page} 页{left.pageCount ? ` / 共 ${left.pageCount} 页` : ""}
              </span>
              <div className="ml-auto flex items-center gap-1">
                <button
                  type="button"
                  disabled={left.page <= 1}
                  onClick={() => onGoToPage(left.page - 1)}
                  className="rounded border border-neutral-300 px-2 py-0.5 text-[11px] disabled:opacity-30 dark:border-neutral-700"
                >
                  ← 上一页
                </button>
                <button
                  type="button"
                  disabled={Boolean(left.pageCount && left.page >= left.pageCount)}
                  onClick={() => onGoToPage(left.page + 1)}
                  className="rounded border border-neutral-300 px-2 py-0.5 text-[11px] disabled:opacity-30 dark:border-neutral-700"
                >
                  下一页 →
                </button>
              </div>
            </>
          ) : (
            <span className="ml-auto text-[11px] text-neutral-400">点下面的论文证据，这里显示那一页</span>
          )}
        </header>

        {paperMode === "pdf" && (
          <div className="flex min-h-0 flex-1 flex-col bg-neutral-100 dark:bg-neutral-900">
            {pdfSearch && (
              <p className="border-b border-neutral-200 bg-white px-3 py-1 text-[11px] text-neutral-500 dark:border-neutral-800 dark:bg-neutral-950">
                PDF 视图已跳到第 {left?.page ?? 1} 页，并用浏览器**内置查找**高亮这段引文
                （浏览器支持范围内；换成「原文文本」能看到精确到字符的高亮）
              </p>
            )}
            {pdfSrc ? (
              // key 里带上页码：只改 URL 的 #fragment 浏览器不会重新加载，换 key 才会真正跳页
              <iframe
                // key 里再带上引文：同一页换一段引文时也要重新加载，`#search=` 才会重新执行
                key={`pdf-${left?.page ?? 1}-${pdfSearch}`}
                src={pdfSrc}
                title="论文 PDF"
                className="h-full w-full border-0"
              />
            ) : (
              <p className="p-3 text-sm text-neutral-500">这份 run 没有可显示的 PDF。</p>
            )}
          </div>
        )}

        <div
          ref={paperBodyRef}
          className={`min-h-0 flex-1 overflow-auto p-3 ${paperMode === "pdf" ? "hidden" : ""}`}
        >
          {!left && (
            <p className="text-sm text-neutral-500">
              还没选内容。在下面任意一条结论里点「第 N 页」的引文，这一页的完整原文就会出现在这里，
              被引用的那句话会被高亮出来。
            </p>
          )}
          {left?.loading && <p className="text-sm text-neutral-500">读取中…</p>}
          {left?.error && <p className="text-sm text-red-600">{left.error}</p>}
          {left && !left.loading && !left.error && (
            <pre className="whitespace-pre-wrap font-mono text-[12px] leading-relaxed">
              {quoteParts ? (
                <>
                  {quoteParts.before}
                  <mark
                    ref={paperMarkRef}
                    className="scroll-mt-24 bg-amber-200 px-0.5 dark:bg-amber-800 dark:text-amber-50"
                  >
                    {quoteParts.match}
                  </mark>
                  {quoteParts.after}
                </>
              ) : (
                left.text
              )}
            </pre>
          )}
          {left && !left.loading && !left.error && left.quote && !quoteParts && (
            <p className="mt-2 text-[11px] text-amber-600">
              注意：引文没有逐字出现在这一页里（可能被截断或换行处被拼接过），所以没有做高亮。
            </p>
          )}
        </div>
      </section>

      {/* ---------------- 右：代码实现 ---------------- */}
      <section className="flex h-[62vh] min-h-[380px] flex-col overflow-hidden rounded-lg border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-950">
        <header className="flex flex-wrap items-center gap-2 border-b border-neutral-200 px-3 py-2 dark:border-neutral-800">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-neutral-500">代码实现</h3>
          {right ? (
            <>
              <span className="truncate font-mono text-[11px] text-neutral-500">
                {right.path}:{right.start}-{right.end}
                {right.total ? `（共 ${right.total} 行）` : ""}
              </span>
              {right.sourceUrl && (
                <a
                  href={right.sourceUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="ml-auto rounded border border-neutral-300 px-2 py-0.5 text-[11px] hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
                >
                  去托管站看 ↗
                </a>
              )}
            </>
          ) : (
            <span className="ml-auto text-[11px] text-neutral-400">点下面的代码引用，这里显示那段代码</span>
          )}
        </header>

        {right?.commit && (
          <p className="border-b border-neutral-200 px-3 py-1 font-mono text-[10px] text-neutral-400 dark:border-neutral-800">
            commit {right.commit.slice(0, 12)}… · 内容与核验时读的是同一份（git 对象）
          </p>
        )}

        <div className="min-h-0 flex-1 overflow-auto p-3">
          {!right && (
            <p className="text-sm text-neutral-500">
              还没选内容。在下面任意一条结论里点 `文件:行号`，那一段代码会出现在这里，
              被引用的行会被高亮，并且**一直保留上下文**（可以往上往下滚看整个文件）。
            </p>
          )}
          {right?.loading && <p className="text-sm text-neutral-500">读取中…</p>}
          {right?.error && <p className="text-sm text-red-600">{right.error}</p>}
          {right && !right.loading && !right.error && (
            <div className="font-mono text-[12px] leading-relaxed">
              {right.lines.map((line) => {
                const inFocus = line.n >= right.start && line.n <= right.end;
                return (
                  <div
                    key={line.n}
                    ref={inFocus && line.n === right.start ? focusRef : undefined}
                    className={`flex gap-3 whitespace-pre ${inFocus ? "bg-amber-100 dark:bg-amber-950/60" : ""}`}
                  >
                    <span className="w-12 shrink-0 select-none text-right text-neutral-400">{line.n}</span>
                    <span className="min-w-0">{line.text || " "}</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {right?.why && (
          <p className="border-t border-neutral-200 px-3 py-2 text-[11px] text-neutral-500 dark:border-neutral-800">
            为什么这段对应那个创新点：{right.why}
          </p>
        )}
      </section>
      </div>
    </div>
  );
}
