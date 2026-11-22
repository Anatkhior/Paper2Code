"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import Icon from "@/components/Icon";
import ThemeToggle from "@/components/ThemeToggle";
import { deleteRun, fetchHistory } from "@/lib/api";
import { HISTORY_STATUS, statusTone } from "@/lib/session";
import type { HistoryEntry } from "@/lib/types";

const STATUS_FILTERS = [
  { label: "全部", value: "" },
  { label: "已完成", value: "complete" },
  { label: "未完/部分", value: "uploaded,running,recon_complete,partial" },
  { label: "失败/中断", value: "failed,cancelled,interrupted,unreadable" },
] as const;

export default function HistoryPage() {
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [result, setResult] = useState<{ items: HistoryEntry[]; total: number; limit: number } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchHistory(query, offset, controller.signal, statusFilter).then(value => {
      if (!controller.signal.aborted) setResult(value);
    }).catch(caught => { if (!controller.signal.aborted) setError(String(caught)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [query, statusFilter, offset, revision]);

  const changePage = (next: number) => { setLoading(true); setError(""); setOffset(next); };
  const changeStatus = (next: string) => { setLoading(true); setError(""); setStatusFilter(next); setOffset(0); };
  const removeRun = async (runId: string) => {
    setDeletingId(runId);
    setError("");
    try {
      await deleteRun(runId);
      setConfirmDeleteId(null);
      setLoading(true);
      setRevision(value => value + 1);
    } catch (caught) {
      setError(String(caught));
    } finally {
      setDeletingId(null);
    }
  };

  return <main className="mx-auto w-full max-w-4xl flex-1 px-4 py-6 sm:px-6 sm:py-8">
    <nav aria-label="主导航" className="mb-6 flex items-center justify-between gap-4 text-sm">
      <Link href="/" className="text-link">← Paper2code</Link>
      <ThemeToggle />
    </nav>
    <header className="mb-8 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0"><h1 className="text-2xl font-semibold tracking-tight">历史解读</h1><p className="mt-2 max-w-[60ch] text-pretty text-sm leading-6 text-muted">每次使用都会自动保存到本机，包括未完成的解读。打开历史后可更换模型继续追问。</p></div>
      <Link href="/" className="primary-button">新建解读</Link>
    </header>
    <form className="mb-4 flex flex-wrap items-end gap-2" onSubmit={event => {
      event.preventDefault(); setLoading(true); setError(""); setQuery(draft.trim()); setOffset(0); setRevision(value => value + 1);
    }}>
      <label className="min-w-0 flex-1 basis-60 text-xs text-muted">按论文标题或文件名查找
        <input value={draft} onChange={event => setDraft(event.target.value)} maxLength={200} type="search" className="field mt-1.5 text-foreground" />
      </label>
      <button className="primary-button" type="submit">搜索</button>
      <button className="secondary-button" type="button" onClick={() => { setLoading(true); setError(""); setRevision(value => value + 1); }}>刷新列表</button>
    </form>
    <div className="mb-6 flex flex-wrap items-center gap-1.5" role="group" aria-label="按状态筛选">
      {STATUS_FILTERS.map(filter => (
        <button key={filter.label} type="button" aria-pressed={statusFilter === filter.value} onClick={() => changeStatus(filter.value)} className="chip">
          {filter.label}
        </button>
      ))}
    </div>
    {error && <p role="alert" className="alert-fail mb-4 rounded-lg px-3 py-2 text-sm">无法完成操作：{error}。请确认后端已启动后重试。</p>}
    {loading ? <p role="status" className="py-8 text-sm text-muted">正在读取历史记录…</p> : result && <>
      <p role="status" className="mb-2 text-xs tabular-nums text-muted">共 {result.total} 份解读</p>
      {!result.items.length ? <p className="panel px-5 py-12 text-center text-sm text-muted">{query || statusFilter ? "没有匹配的论文，试试调整关键词或状态筛选。" : "本机还没有历史解读。新建一次解读后会自动出现在这里。"}</p> :
        <ol className="panel divide-y divide-line overflow-hidden">
          {result.items.map(item => <li key={item.run_id} className="px-4 py-4 transition-colors hover:bg-subtle sm:px-5">
            <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
              <div className="min-w-0 flex-1">
                <Link href={`/?run=${encodeURIComponent(item.run_id)}`} prefetch={false} className="break-words text-[0.9375rem] font-medium text-foreground underline-offset-4 hover:text-accent hover:underline">{item.title}</Link>
                {item.filename && item.filename !== item.title && <p className="mt-0.5 break-words text-xs text-muted">{item.filename}</p>}
              </div>
              <div className="flex items-center gap-2">
                <span data-tone={statusTone(item.analysis_status)} className="badge">{HISTORY_STATUS[item.analysis_status] || item.analysis_status}</span>
                {item.analysis_status !== "running" && (confirmDeleteId === item.run_id ? (
                  <span className="inline-flex items-center gap-1">
                    <button type="button" autoFocus disabled={deletingId === item.run_id} onClick={() => void removeRun(item.run_id)} className="danger-button">{deletingId === item.run_id ? "删除中…" : "确认删除"}</button>
                    <button type="button" disabled={deletingId === item.run_id} onClick={() => setConfirmDeleteId(null)} className="quiet-button text-muted">保留</button>
                  </span>
                ) : (
                  <button type="button" onClick={() => setConfirmDeleteId(item.run_id)} aria-label={`删除历史「${item.title}」`} className="quiet-button text-muted hover:text-fail">
                    <Icon name="trash" size={13} />删除
                  </button>
                ))}
              </div>
            </div>
            <p className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
              <span className="tabular-nums">{new Date(item.updated_at * 1000).toLocaleString()}</span>
              <span className="tabular-nums">{item.innovation_count ?? 0} 条创新点 · {item.result_count ?? 0} 条结论</span>
              <span>原模型：{item.original_provider?.model || "未记录"}</span>
              {item.latest_operation?.phase === "chat" && <span>最近追问：{item.latest_operation.status === "ok" ? "已完成" : HISTORY_STATUS[item.latest_operation.status] || item.latest_operation.status}</span>}
              <span className="font-mono text-[11px]" title="解读编号">{item.run_id}</span>
            </p>
            {item.warnings?.length > 0 && <p className="mt-2 text-xs text-warn">部分记录需要检查；打开后可查看详情。</p>}
          </li>)}
        </ol>}
      <nav aria-label="历史分页" className="mt-5 flex items-center justify-between gap-3 text-sm">
        <button type="button" disabled={!offset} onClick={() => changePage(Math.max(0, offset - result.limit))} className="secondary-button">上一页</button>
        <span className="tabular-nums text-muted">第 {Math.floor(offset / result.limit) + 1} / {Math.max(1, Math.ceil(result.total / result.limit))} 页</span>
        <button type="button" disabled={offset + result.limit >= result.total} onClick={() => changePage(offset + result.limit)} className="secondary-button">下一页</button>
      </nav>
    </>}
  </main>;
}
