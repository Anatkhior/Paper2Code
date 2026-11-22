"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";

const PdfPage = dynamic(() => import("./PdfPage"), { ssr: false });
import Icon from "@/components/Icon";
import { highlightCode } from "@/lib/syntax";
import Markdown from "@/components/Markdown";
import { placeCodeAnnotations, type CodeAnnotation, type WalkthroughStep } from "@/lib/codeAnnotations";

const ZOOM_LEVELS = [1, 1.25, 1.5, 2] as const;

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
  /** 页面尺寸（PDF 点）与引文高亮矩形（PDF 点），原版页面视图用它们叠高亮框 */
  pageWidth?: number;
  pageHeight?: number;
  rects?: number[][];
  /** 高亮覆盖率：<1 表示引文里有公式/符号之类在 PDF 文本层匹配不到的部分 */
  coverage?: number | null;
  loading: boolean;
  error?: string | null;
}

type PaperMode = "pdf" | "text";

interface Props {
  left: PaperPane | null;
  right: CodePane | null;
  walkthrough?: WalkthroughStep[];
  onOpenCode: (path: string, start: number, end: number, why: string) => void;
  activeTab: "paper" | "code";
  onTabChange: (tab: "paper" | "code") => void;
  codeEmpty: string;
  /** 论文 PDF 的直链：给"在新标签页打开原版"用（不再是嵌入方式，见下） */
  pdfUrl?: string | null;
  /** 论文页渲染图的地址前缀（服务端渲染，前端叠高亮框） */
  pageImageUrl?: ((page: number) => string) | null;
  onGoToPage: (page: number) => void;
  /** 用户划选原文后，把这一段作为定位目标加进清单 */
  onSelectTarget?: (quote: string, page: number) => void;
}

function Annotation({ annotation, onOpenCode }: { annotation: CodeAnnotation; onOpenCode: Props["onOpenCode"] }) {
  const { reference, notice } = annotation;
  return <aside className="code-annotation" data-source="explanation">
    <div className="code-annotation-meta">
      <span>AI 讲解 · 非仓库原文</span>
      {!notice && reference && <code title={annotation.line_ref}>L{reference.start}{reference.end !== reference.start ? `–${reference.end}` : ""}</code>}
    </div>
    {notice && <div className="code-annotation-location">
      <span>{notice} · </span>
      {reference ? <button type="button" className="underline underline-offset-4" onClick={() => onOpenCode(reference.path, reference.start, reference.end, "来自当前创新点的 AI 讲解")}>{annotation.line_ref}</button> : <code>{annotation.line_ref}</code>}
    </div>}
    <div className="code-annotation-text"><Markdown text={annotation.text} /></div>
  </aside>;
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

/** 桌面并排对照，窄屏切换；窗格内部滚动不带动页面。 */
export default function Reader({
  left, right, walkthrough, onOpenCode, activeTab, onTabChange, codeEmpty,
  pdfUrl, pageImageUrl, onGoToPage, onSelectTarget,
}: Props) {
  const [paperMode, setPaperMode] = useState<PaperMode>("pdf");
  const [paperZoom, setPaperZoom] = useState<number>(1);
  const [pageDraft, setPageDraft] = useState<{ page: number; value: string } | null>(null);
  const [copiedCodeRef, setCopiedCodeRef] = useState(false);
  const focusRef = useRef<HTMLDivElement>(null);
  const codeBodyRef = useRef<HTMLDivElement>(null);
  const paperMarkRef = useRef<HTMLElement>(null);
  const pdfBoxRef = useRef<HTMLDivElement>(null);
  const pdfSurfaceRef = useRef<HTMLDivElement>(null);
  const pdfMarkRef = useRef<HTMLSpanElement>(null);
  const syntax = useMemo(() => highlightCode(right?.path ?? "", right?.lines.map((line) => line.text).join("\n") ?? ""), [right?.path, right?.lines]);
  const annotations = useMemo(() => placeCodeAnnotations(walkthrough, right?.lines.length ? {
    path: right.path, start: right.lines[0].n, end: right.lines[right.lines.length - 1].n, total: right.total,
  } : null), [walkthrough, right]);
  const paperBodyRef = useRef<HTMLDivElement>(null);
  const selectionOwner = JSON.stringify([pdfUrl, left?.page, left?.quote, paperMode, paperZoom]);
  const [selection, setSelection] = useState<{ text: string; x: number; y: number; page: number; owner: string } | null>(null);
  const currentSelection = selection?.owner === selectionOwner ? selection : null;
  const quoteParts = useMemo(
    () => (left?.text ? locateQuote(left.text, left.quote) : null),
    [left],
  );

  const centerCodeFocus = useCallback(() => {
    const box = codeBodyRef.current;
    const target = focusRef.current;
    if (box && target) box.scrollTop += target.getBoundingClientRect().top - box.getBoundingClientRect().top - box.clientHeight / 2;
  }, []);

  useEffect(() => {
    centerCodeFocus();
  }, [centerCodeFocus, right?.path, right?.start, right?.end, right?.lines, activeTab, walkthrough]);

  const stepZoom = (delta: number) => {
    const index = ZOOM_LEVELS.findIndex((level) => level >= paperZoom);
    const current = index < 0 ? ZOOM_LEVELS.length - 1 : index;
    const next = Math.min(ZOOM_LEVELS.length - 1, Math.max(0, current + delta));
    setPaperZoom(ZOOM_LEVELS[next]);
  };

  const commitPageJump = () => {
    if (!left || !pageDraft) return;
    const parsed = Number.parseInt(pageDraft.value.trim(), 10);
    setPageDraft(null);
    if (!Number.isFinite(parsed)) return;
    const clamped = Math.min(left.pageCount ?? parsed, Math.max(1, parsed));
    if (clamped !== left.page) onGoToPage(clamped);
  };

  const copyCodeRef = async () => {
    if (!right) return;
    const ref = `${right.path}:${right.start}${right.end !== right.start ? `-${right.end}` : ""}`;
    try {
      await navigator.clipboard.writeText(ref);
      setCopiedCodeRef(true);
      window.setTimeout(() => setCopiedCodeRef(false), 1600);
    } catch { /* ignore clipboard errors */ }
  };

  /**
   * 左栏的高亮自动进视野。
   *
   * 用户不用自己滚：点了「查看原文」→ 页面滚到阅读器 → 这一页的文本可能刚好加载完，
   * 高亮又落在滚动区外面。这里在引文/该页文本/视图模式变化时把 <mark> 挪到可视区中间。
   */
  useEffect(() => {
    if (!quoteParts) return;
    const timer = window.setTimeout(() => {
      const box = paperBodyRef.current;
      const mark = paperMarkRef.current;
      if (box && mark) box.scrollTop += mark.getBoundingClientRect().top - box.getBoundingClientRect().top - box.clientHeight / 2;
    }, 60);
    return () => window.clearTimeout(timer);
  }, [left?.quote, left?.text, left?.page, paperMode, quoteParts, activeTab]);


  /**
   * 划选一段原文 → 弹出「以此为目标」。
   * 只认**左栏里**的选中内容：右栏是代码，划选它不该触发这个动作。
   */
  const handleSelection = useCallback(() => {
    const active = window.getSelection();
    const text = active?.toString().trim() ?? "";
    const container = paperMode === "pdf" ? pdfSurfaceRef.current?.querySelector(".textLayer") : paperBodyRef.current;
    if (!active?.rangeCount || text.length < 8 || !container || !left) {
      setSelection(null);
      return;
    }
    const range = active.getRangeAt(0);
    if (!container.contains(range.startContainer) || !container.contains(range.endContainer)) {
      setSelection(null);
      return;
    }
    const rect = range.getBoundingClientRect();
    setSelection({
      text, page: left.page, owner: selectionOwner,
      x: Math.min(Math.max(rect.left + rect.width / 2, 130), window.innerWidth - 130), y: rect.top,
    });
  }, [left, paperMode, selectionOwner]);

  useEffect(() => {
    // 仅清理位于论文阅读器内的旧选区，不误伤用户在追问/结论里选中的文字。
    const active = window.getSelection();
    if (active?.rangeCount) {
      const range = active.getRangeAt(0);
      if (pdfSurfaceRef.current?.contains(range.startContainer) || paperBodyRef.current?.contains(range.startContainer)) {
        active.removeAllRanges();
      }
    }
    const changed = () => handleSelection();
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setSelection(null); window.getSelection()?.removeAllRanges(); }
    };
    const clearOnScroll = (event: Event) => {
      const target = event.target;
      if (target === document || target === document.documentElement || target === pdfBoxRef.current || target === paperBodyRef.current) {
        setSelection(null);
      }
    };
    document.addEventListener("scroll", clearOnScroll, true);
    document.addEventListener("selectionchange", changed);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("scroll", clearOnScroll, true); document.removeEventListener("selectionchange", changed); document.removeEventListener("keydown", escape); };
  }, [handleSelection]);

  // 高亮与无边框图像共享同一坐标面，所有矩形随页面等比例缩放。
  const pageWidth = left?.pageWidth ?? 0;
  const pageHeight = left?.pageHeight ?? 0;
  const imageUrl = pageImageUrl && left ? pageImageUrl(left.page) : null;
  // **每个矩形都要画**：后端把引文按行拆成多个矩形（换行一段一个），
  // 之前只画 rects[0]，换行后的句子就丢了高亮（2026-09-17 用户实测）。
  const highlightStyles = useMemo(() =>
    pageWidth > 0 && pageHeight > 0
      ? (left?.rects ?? []).map((rect) => ({
          left: `${(rect[0] / pageWidth) * 100}%`,
          top: `${(rect[1] / pageHeight) * 100}%`,
          width: `${((rect[2] - rect[0]) / pageWidth) * 100}%`,
          height: `${((rect[3] - rect[1]) / pageHeight) * 100}%`,
        }))
      : [], [left?.rects, pageWidth, pageHeight]);

  const centerPdf = useCallback(() => {
    const box = pdfBoxRef.current;
    const mark = pdfMarkRef.current;
    if (box && mark && box.clientHeight) {
      box.scrollTop += mark.getBoundingClientRect().top - box.getBoundingClientRect().top - box.clientHeight / 2;
    }
  }, []);

  useEffect(() => {
    if (paperMode !== "pdf" || !pdfSurfaceRef.current) return;
    const observer = new ResizeObserver(centerPdf);
    observer.observe(pdfSurfaceRef.current);
    centerPdf();
    return () => observer.disconnect();
  }, [centerPdf, paperMode, highlightStyles, activeTab]);

  return (
    <div className="relative">
      <div className="mb-2 flex gap-2 lg:hidden" role="group" aria-label="阅读内容">
        <button type="button" aria-pressed={activeTab === "paper"} aria-controls="paper-pane" onClick={() => onTabChange("paper")} className="reader-tab">论文原文</button>
        <button type="button" aria-pressed={activeTab === "code"} aria-controls="code-pane" onClick={() => onTabChange("code")} className="reader-tab">代码实现</button>
      </div>
      <div className="grid items-start gap-3 lg:grid-cols-2" onPointerUp={handleSelection} onKeyUp={handleSelection}>
      {currentSelection && onSelectTarget && (
        <button
          type="button"
          style={{
            position: "fixed",
            left: currentSelection.x,
            top: Math.max(8, currentSelection.y - 44),
            transform: "translateX(-50%)",
          }}
          className="primary-button z-40 min-h-9 px-3 py-1.5 text-xs shadow-[0_8px_24px_-8px_rgb(0_0_0/0.45)]"
          onPointerDown={(event) => event.preventDefault()}
          disabled={currentSelection.text.length > 1200}
          onClick={() => {
            onSelectTarget(currentSelection.text, currentSelection.page);
            setSelection(null);
            window.getSelection()?.removeAllRanges();
          }}
        >
          {currentSelection.text.length > 1200 ? "选段超过 1200 字，请缩短后重试" : "以此为目标定位代码"}
        </button>
      )}
      <section id="paper-pane" className={`reader-pane panel ${activeTab !== "paper" ? "reader-mobile-hidden" : ""}`}>
        <header className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
          <h3 className="text-sm font-semibold">论文原文</h3>
          {left && <>
            <form
              className="flex items-center gap-1 text-xs tabular-nums text-muted"
              onSubmit={(event) => { event.preventDefault(); commitPageJump(); }}
            >
              <span>第</span>
              <input
                type="text"
                inputMode="numeric"
                aria-label="跳转到指定页"
                disabled={left.loading}
                value={pageDraft?.page === left.page ? pageDraft.value : String(left.page)}
                onChange={(event) => setPageDraft({ page: left.page, value: event.target.value.replace(/[^\d]/g, "") })}
                onBlur={commitPageJump}
                onKeyDown={(event) => { if (event.key === "Escape") { setPageDraft(null); event.currentTarget.blur(); } }}
                className="page-jump"
              />
              <span>页{left.pageCount ? ` / ${left.pageCount}` : ""}</span>
            </form>
            <div className="ml-auto flex gap-1">
              <button type="button" disabled={left.loading || left.page <= 1} onClick={() => onGoToPage(left.page - 1)} className="reader-control">上一页</button>
              <button type="button" disabled={left.loading || Boolean(left.pageCount && left.page >= left.pageCount)} onClick={() => onGoToPage(left.page + 1)} className="reader-control">下一页</button>
            </div>
          </>}
        </header>
        <div className="flex flex-wrap items-center gap-1 border-b border-line px-3 py-1" role="group" aria-label="原文格式">
          <button type="button" aria-pressed={paperMode === "pdf"} onClick={() => setPaperMode("pdf")} className="reader-tab">PDF 原版</button>
          <button type="button" aria-pressed={paperMode === "text"} onClick={() => setPaperMode("text")} className="reader-tab">原文文本（备用）</button>
          {paperMode === "pdf" && (
            <span className="inline-flex items-center gap-0.5">
              <button type="button" aria-label="缩小原文" disabled={paperZoom <= ZOOM_LEVELS[0]} onClick={() => stepZoom(-1)} className="reader-control px-2">−</button>
              <span className="min-w-10 text-center text-xs tabular-nums text-muted">{Math.round(paperZoom * 100)}%</span>
              <button type="button" aria-label="进一步放大原文" disabled={paperZoom >= ZOOM_LEVELS[ZOOM_LEVELS.length - 1]} onClick={() => stepZoom(1)} className="reader-control px-2">+</button>
              <button type="button" aria-label={paperZoom === 1 ? "放大原文（两倍）" : "原文适合宽度"} onClick={() => setPaperZoom(paperZoom === 1 ? 2 : 1)} className="reader-control">{paperZoom === 1 ? "放大" : "适合宽度"}</button>
            </span>
          )}
          {pdfUrl && <a href={`${pdfUrl}#page=${left?.page ?? 1}`} target="_blank" rel="noreferrer" className="text-link ml-auto py-2 text-xs underline">打开 PDF</a>}
        </div>
        {!left && <p className="p-4 text-sm text-muted">选择创新点，阅读对应原文。</p>}
        {left?.loading && <p className="p-4 text-sm text-muted" role="status">读取论文中…</p>}
        {left?.error && <p className="p-4 text-sm text-fail" role="alert">{left.error}</p>}
        {left && !left.loading && !left.error && <>
          <p className="px-3 py-2 text-xs text-muted">
            {paperMode === "text" ? "划选一段原文，可将它加入定位目标。" : !left.quote ? "直接划选 PDF 原文，可将它加入定位目标。" : highlightStyles.length === 0 ? "本页未定位到这段引文，可切换原文文本核对。" : (left.coverage ?? 1) < 0.999 ? `引文已高亮约 ${Math.round((left.coverage ?? 0) * 100)}%，部分公式或符号未匹配。` : "引文已高亮"}
          </p>
          {paperMode === "pdf" && <div ref={pdfBoxRef} className="reader-body min-h-0 bg-subtle p-3">
            {imageUrl && <div style={{ width: `${paperZoom * 100}%` }} className="mx-auto overflow-hidden rounded-sm shadow-[0_1px_3px_rgb(0_0_0/0.12),0_4px_14px_-6px_rgb(0_0_0/0.18)]">
              <div ref={pdfSurfaceRef} className="pdf-surface relative">
                {pdfUrl ? <PdfPage url={pdfUrl} page={left.page} fallbackUrl={imageUrl} onReady={centerPdf}>
                  {highlightStyles.map((style, index) => <span key={index} ref={index === 0 ? pdfMarkRef : undefined} className="pdf-highlight pointer-events-none absolute bg-amber-300/35" style={style} aria-hidden />)}
                </PdfPage> : <p className="p-3 text-sm">没有可用的 PDF，请重新上传。</p>}
              </div>
            </div>}
          </div>}
          <div ref={paperBodyRef} className={`reader-body min-h-0 p-3 ${paperMode === "pdf" ? "hidden" : ""}`}>
            <pre className="whitespace-pre-wrap font-mono text-xs leading-relaxed">
              {quoteParts ? <>{quoteParts.before}<mark ref={paperMarkRef} className="bg-amber-200 dark:bg-amber-800 dark:text-amber-50">{quoteParts.match}</mark>{quoteParts.after}</> : left.text}
            </pre>
            {left.quote && !quoteParts && <p className="mt-2 text-xs text-warn">引文未逐字出现在本页，未添加文本高亮。</p>}
          </div>
        </>}
      </section>

      <section id="code-pane" className={`reader-pane panel ${activeTab !== "code" ? "reader-mobile-hidden" : ""}`}>
        <header className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
          <h3 className="text-sm font-semibold">代码实现</h3>
          {right && <span className="text-xs text-muted">{syntax.language}</span>}
          {right && !right.loading && !right.error && (
            <div className="ml-auto flex items-center gap-1">
              <button type="button" onClick={centerCodeFocus} className="reader-control text-muted">回到引用行</button>
              <button type="button" onClick={() => void copyCodeRef()} className="reader-control inline-flex items-center gap-1 text-muted">
                <Icon name={copiedCodeRef ? "check" : "copy"} size={12} />{copiedCodeRef ? "已复制" : "复制位置"}
              </button>
              {right.sourceUrl && <a href={right.sourceUrl} target="_blank" rel="noreferrer" className="text-link px-1.5 py-2 text-xs underline">打开源文件</a>}
            </div>
          )}
          {right && (right.loading || right.error) && right.sourceUrl && <a href={right.sourceUrl} target="_blank" rel="noreferrer" className="text-link ml-auto py-2 text-xs underline">打开源文件</a>}
        </header>
        {!right && <p className="p-4 text-sm leading-6 text-muted">{codeEmpty}</p>}
        {!right && annotations.unplaced.length > 0 && <div className="reader-body code-surface">
          {annotations.unplaced.map((annotation) => <Annotation key={annotation.id} annotation={annotation} onOpenCode={onOpenCode} />)}
        </div>}
        {right && <>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-3 py-2 text-xs">
            <span className="min-w-0 break-all font-mono">{right.path}</span>
            <span className="text-muted">引用 L{right.start}–{right.end}{right.total ? ` / 共 ${right.total} 行` : ""}</span>
            {right.total && right.lines.length > 0 && right.lines.length < right.total && <span className="text-muted">当前显示 L{right.lines[0].n}–{right.lines[right.lines.length - 1].n}</span>}
            {right.commit && <span className="ml-auto font-mono text-muted" title={`核验版本 ${right.commit}`}>{right.commit.slice(0, 8)}</span>}
          </div>
          <div ref={codeBodyRef} className="reader-body code-surface min-h-0 py-3">
            {right.loading && <p className="px-3 text-sm text-muted" role="status">读取代码中…</p>}
            {right.error && <p className="px-3 text-sm text-fail" role="alert">{right.error}</p>}
            {!right.loading && !right.error && <div className="w-max min-w-full font-mono text-xs leading-6">
              {right.lines.map((line, index) => {
                const inFocus = line.n >= right.start && line.n <= right.end;
                return <div key={line.n} ref={line.n === right.start ? focusRef : undefined}>
                  {annotations.byLine.get(line.n)?.map((annotation) => <Annotation key={annotation.id} annotation={annotation} onOpenCode={onOpenCode} />)}
                  <div className="code-line flex gap-4 whitespace-pre pr-4" data-cited={inFocus || undefined}>
                    <span className="code-line-number w-12 shrink-0 select-none pr-2 text-right" aria-label={inFocus ? `引用行 ${line.n}` : undefined}>{line.n}</span>
                    <code>{syntax.lines[index]?.map((part, partIndex) => <span key={partIndex} className={part.types.length ? `token ${part.types.join(" ")}` : undefined}>{part.text}</span>)}</code>
                  </div>
                </div>;
              })}
              {annotations.unplaced.map((annotation) => <Annotation key={annotation.id} annotation={annotation} onOpenCode={onOpenCode} />)}
            </div>}
          </div>
          {right.why && <Markdown text={right.why} className="border-t border-line px-3 py-2 text-xs leading-6 text-muted" />}
        </>}
      </section>
      </div>
    </div>
  );
}
