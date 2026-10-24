"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { fetchHistory } from "@/lib/api";
import { HISTORY_STATUS } from "@/lib/session";
import type { HistoryEntry } from "@/lib/types";

export default function HistoryPage() {
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [result, setResult] = useState<{ items: HistoryEntry[]; total: number; limit: number } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    fetchHistory(query, offset, controller.signal).then(value => {
      if (!controller.signal.aborted) setResult(value);
    }).catch(caught => { if (!controller.signal.aborted) setError(String(caught)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [query, offset, revision]);
  const changePage = (next: number) => { setLoading(true); setError(""); setOffset(next); };

  return <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-6 sm:px-6">
    <header className="mb-8 flex flex-wrap items-start justify-between gap-4">
      <div><h1 className="text-2xl font-semibold">历史解读</h1><p className="mt-2 text-sm text-neutral-600 dark:text-neutral-400">每次使用都会自动保存到本机，包括未完成的解读。打开历史后可更换模型继续追问。</p></div>
      <Link href="/" className="primary-button">新建解读</Link>
    </header>
    <form className="mb-5 flex flex-wrap items-end gap-2" onSubmit={event => {
      event.preventDefault(); setLoading(true); setError(""); setQuery(draft.trim()); setOffset(0); setRevision(value => value + 1);
    }}>
      <label className="min-w-0 flex-1 text-sm">按论文标题或文件名查找
        <input value={draft} onChange={event => setDraft(event.target.value)} maxLength={200} type="search" className="mt-2 block min-h-11 w-full rounded-lg border border-neutral-300 bg-transparent px-3 dark:border-neutral-700" />
      </label>
      <button className="primary-button" type="submit">搜索</button>
      <button className="min-h-11 rounded-lg border border-neutral-300 px-3 text-sm dark:border-neutral-700" type="button" onClick={() => { setLoading(true); setError(""); setRevision(value => value + 1); }}>刷新列表</button>
    </form>
    {error && <p role="alert" className="mb-4 text-sm text-red-700 dark:text-red-300">无法读取历史：{error}。请确认后端已启动后重试。</p>}
    {loading ? <p role="status" className="py-8 text-sm text-neutral-600 dark:text-neutral-400">正在读取历史记录…</p> : result && <>
      <p role="status" className="mb-2 text-sm text-neutral-600 dark:text-neutral-400">共 {result.total} 份解读</p>
      {!result.items.length ? <p className="border-y border-neutral-200 py-12 text-sm dark:border-neutral-800">{query ? "没有匹配的论文，试试其他标题或文件名。" : "本机还没有历史解读。新建一次解读后会自动出现在这里。"}</p> :
        <ol className="divide-y divide-neutral-200 border-y border-neutral-200 dark:divide-neutral-800 dark:border-neutral-800">
          {result.items.map(item => <li key={item.run_id} className="py-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <Link href={`/?run=${encodeURIComponent(item.run_id)}`} prefetch={false} className="break-words text-base font-medium text-teal-800 underline-offset-4 hover:underline dark:text-teal-300">{item.title}</Link>
                {item.filename && item.filename !== item.title && <p className="mt-1 break-words text-xs text-neutral-600 dark:text-neutral-400">{item.filename}</p>}
              </div>
              <span className="text-sm text-neutral-700 dark:text-neutral-300">{HISTORY_STATUS[item.analysis_status] || item.analysis_status}</span>
            </div>
            <p className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-neutral-600 dark:text-neutral-400">
              <span>更新于 {new Date(item.updated_at * 1000).toLocaleString()}</span>
              <span>{item.innovation_count ?? 0} 条创新点 · {item.result_count ?? 0} 条结论</span>
              <span>原模型：{item.original_provider?.model || "未记录"}</span>
              {item.latest_operation?.phase === "chat" && <span>最近追问：{item.latest_operation.status === "ok" ? "已完成" : HISTORY_STATUS[item.latest_operation.status] || item.latest_operation.status}</span>}
            </p>
            {item.warnings?.length > 0 && <p className="mt-2 text-xs text-amber-800 dark:text-amber-300">部分记录需要检查；打开后可查看详情。</p>}
          </li>)}
        </ol>}
      <nav aria-label="历史分页" className="mt-5 flex items-center justify-between gap-3 text-sm">
        <button type="button" disabled={!offset} onClick={() => changePage(Math.max(0, offset - result.limit))} className="min-h-11 rounded-lg border border-neutral-300 px-4 disabled:opacity-40 dark:border-neutral-700">上一页</button>
        <span>第 {Math.floor(offset / result.limit) + 1} / {Math.max(1, Math.ceil(result.total / result.limit))} 页</span>
        <button type="button" disabled={offset + result.limit >= result.total} onClick={() => changePage(offset + result.limit)} className="min-h-11 rounded-lg border border-neutral-300 px-4 disabled:opacity-40 dark:border-neutral-700">下一页</button>
      </nav>
    </>}
  </main>;
}
