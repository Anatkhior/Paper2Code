"use client";

import { useCallback, useEffect, useState } from "react";
import type { CodePane, PaperPane } from "@/components/Reader";
import { fetchFile, fetchPaperPage } from "./api";
import type { Finding, Innovation } from "./types";

type PaperTarget = { page: number; quote: string };
type CodeTarget = { path: string; start: number; end: number; why: string };
type Override<T> = { owner: string; target: T } | null;

/** 自动引用和手动引用共用一个请求路径；结果只属于发起请求时的 run / 创新点 / 引用 / 仓库版本。 */
export function useReader(runId: string | null, active: Innovation | undefined, finding: Finding | undefined, repoVersion: number) {
  const owner = JSON.stringify([runId, active?.id]);
  const [paperOverride, setPaperOverride] = useState<Override<PaperTarget>>(null);
  const [codeOverride, setCodeOverride] = useState<Override<CodeTarget>>(null);
  const evidence = finding?.paper_evidence[0] ?? active?.paper_evidence[0];
  const code = finding?.code_evidence[0];
  const paperTarget = paperOverride?.owner === owner ? paperOverride.target : { page: evidence?.page ?? 1, quote: evidence?.quote ?? "" };
  const codeTarget = codeOverride?.owner === owner ? codeOverride.target : code ? { path: code.path, start: code.line_start, end: code.line_end, why: code.why } : null;
  const { page, quote } = paperTarget;
  const { path, start, end, why } = codeTarget ?? {};
  const paperKey = JSON.stringify([owner, page, quote]);
  const codeKey = JSON.stringify([owner, repoVersion, path, start, end, why]);
  const [paperResult, setPaperResult] = useState<{ key: string; pane: PaperPane } | null>(null);
  const [codeResult, setCodeResult] = useState<{ key: string; pane: CodePane } | null>(null);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    fetchPaperPage(runId, page, quote).then((view) => {
      if (cancelled) return;
      setPaperResult({ key: paperKey, pane: {
        page, quote, text: view.text, pageCount: view.page_count,
        pageWidth: view.page_width, pageHeight: view.page_height,
        rects: view.highlight_rects, coverage: view.highlight_coverage, loading: false,
      } });
    }).catch((error: unknown) => {
      if (!cancelled) setPaperResult({ key: paperKey, pane: { page, quote, loading: false, error: String(error) } });
    });
    return () => { cancelled = true; };
  }, [runId, page, quote, paperKey]);

  useEffect(() => {
    if (!runId || path === undefined || start === undefined || end === undefined) return;
    let cancelled = false;
    fetchFile(runId, path, start, end).then((view) => {
      if (cancelled) return;
      setCodeResult({ key: codeKey, pane: {
        path: view.path, start: view.focus.start, end: view.focus.end, why,
        lines: view.lines, total: view.total_lines, commit: view.commit_sha,
        sourceUrl: view.source_url, loading: false,
      } });
    }).catch((error: unknown) => {
      if (!cancelled) setCodeResult({ key: codeKey, pane: { path, start, end, why, lines: [], loading: false, error: String(error) } });
    });
    return () => { cancelled = true; };
  }, [runId, path, start, end, why, codeKey]);

  const reset = useCallback(() => { setPaperOverride(null); setCodeOverride(null); }, [setPaperOverride, setCodeOverride]);

  return {
    paperPane: !runId ? null : paperResult?.key === paperKey ? paperResult.pane : { ...paperTarget, loading: true },
    codePane: !runId || !codeTarget ? null : codeResult?.key === codeKey ? codeResult.pane : { ...codeTarget, lines: [], loading: true },
    selectPaper: (target: PaperTarget) => setPaperOverride({ owner, target }),
    selectCode: (target: CodeTarget) => setCodeOverride({ owner, target }),
    reset,
  };
}
