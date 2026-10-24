"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { chatState } from "@/lib/chatState";
import { migrateStorage, PROVIDER_STORAGE_KEY, TIMELINE_STORAGE_KEY } from "@/lib/providerStorage";
import { DEFAULT_OUTPUT_LANGUAGE, normalizeOutputLanguage, OUTPUT_LANGUAGE_STORAGE_KEY } from "@/lib/language";
import { HISTORY_STATUS, providerChange } from "@/lib/session";
import { useRunSession } from "@/lib/useRunSession";
import ChatPanel from "@/components/ChatPanel";
import Markdown from "@/components/Markdown";
import ComparePanel from "@/components/ComparePanel";
import CoverageCard from "@/components/CoverageCard";
import PlanList, { PlanDetail } from "@/components/PlanList";
import ProviderForm from "@/components/ProviderForm";
import Reader from "@/components/Reader";
import { useReader } from "@/lib/useReader";
import Timeline from "@/components/Timeline";
import { addPlanItem, API_BASE, createRun, deletePlanItem, healthUrl, paperPageImageUrl,
  patchPlanItem, pdfUrl, postChat, smokeTest, startLocate, startRecon } from "@/lib/api";
import type { OutputLanguage, ProviderConfig, SmokeResult } from "@/lib/types";

const EMPTY_PROVIDER: ProviderConfig = { protocol: "openai-compatible", base_url: "", api_key: "", model: "" };
const EMPTY_EVENTS: import("@/lib/types").RunEvent[] = [];

export default function Home() {
  const session = useRunSession();
  const { snapshot, load, reset, refresh, connect, updatePlan, isCurrent } = session;
  const runId = snapshot?.run_id ?? null;
  const paper = snapshot?.paper ?? null;
  const plan = snapshot?.plan ?? null;
  const events = snapshot?.events ?? EMPTY_EVENTS;
  const findings = snapshot?.findings ?? [];
  const chatTurns = snapshot?.turns ?? [];
  const [readOnly, setReadOnly] = useState(false);
  const [pendingPhase, setPendingPhase] = useState<string | null>(null);
  const [chatScope, setChatScope] = useState<"current" | "all">("current");
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [provider, setProvider] = useState<ProviderConfig>(EMPTY_PROVIDER);
  const [outputLanguage, setOutputLanguage] = useState<OutputLanguage>(DEFAULT_OUTPUT_LANGUAGE);
  const [rememberKey, setRememberKey] = useState(false);
  const [smoke, setSmoke] = useState<SmokeResult | null>(null);
  const [smokeBusy, setSmokeBusy] = useState(false);
  const smokeRevision = useRef(0);
  const actionGeneration = useRef(0);
  const [backendUp, setBackendUp] = useState<boolean | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [repoUrl, setRepoUrl] = useState("");
  const [timelineOpen, setTimelineOpen] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [setupOpen, setSetupOpen] = useState(true);
  const [readerOpen, setReaderOpen] = useState(true);
  const [readerTab, setReaderTab] = useState<"paper" | "code">("paper");
  const [activeId, setActiveId] = useState("");
  const chatRef = useRef<HTMLDivElement>(null);
  const readerRef = useRef<HTMLDivElement>(null);
  const activeInnovation = plan?.innovations.find(item => item.id === activeId) ?? plan?.innovations[0];
  const activeFinding = findings.find(item => item.id === activeInnovation?.id);
  const repoVersion = snapshot?.repo?.commit_sha ?? "";
  const { paperPane, codePane, selectPaper, selectCode, reset: clearReader } = useReader(runId, activeInnovation, activeFinding, repoVersion);
  const operation = snapshot?.history.latest_operation;
  const phase = pendingPhase ?? (snapshot?.active ? operation?.phase ?? "recon" : runId ? "done" : "idle");
  const chatBusy = phase === "chat";
  const busy = session.loading || ["uploading", "recon", "locate"].includes(phase);
  const locateBusy = pendingPhase === "locate";
  const chatContext = chatScope === "current" && activeInnovation ? { id: activeInnovation.id, name: activeInnovation.name } : null;
  const chatProgress = useMemo(() => chatState(events), [events]);
  const chatStreaming = chatBusy ? chatProgress.streaming : "";
  const verification = snapshot?.verification ?? null;
  const missingIds = snapshot?.artifact?.missing_ids ?? [];
  const filesTotal = snapshot?.repo?.files_total ?? null;
  const commitSha = snapshot?.repo?.commit_sha ?? null;
  const runEnd = [...events].reverse().find(event => event.type === "run_end")?.data;
  const paperReady = paper;

  useEffect(() => {
    let mounted = true;
    void fetch(healthUrl()).then(response => { if (mounted) setBackendUp(response.ok); })
      .catch(() => { if (mounted) setBackendUp(false); });
    // Browser-only preferences and URL restoration run once after hydration.
    queueMicrotask(() => {
      if (!mounted) return;
      try {
        migrateStorage(window.localStorage);
        const raw = window.localStorage.getItem(PROVIDER_STORAGE_KEY);
        if (raw) {
          const saved = JSON.parse(raw);
          setProvider({ protocol: saved.protocol === "anthropic" ? "anthropic" : "openai-compatible",
            base_url: typeof saved.base_url === "string" ? saved.base_url : "",
            model: typeof saved.model === "string" ? saved.model : "",
            api_key: saved.remember_key === true && typeof saved.api_key === "string" ? saved.api_key : "" });
          setRememberKey(saved.remember_key === true);
        }
        setOutputLanguage(normalizeOutputLanguage(window.localStorage.getItem(OUTPUT_LANGUAGE_STORAGE_KEY)));
        setTimelineOpen(window.localStorage.getItem(TIMELINE_STORAGE_KEY) !== "0");
      } catch { setNotice("浏览器设置无法读取，请重新填写模型设置；本机历史不受影响。"); }
      const id = new URLSearchParams(window.location.search).get("run");
      if (id) { setReadOnly(true); setSetupOpen(false); void load(id); }
    });
    // Restore through a fresh GET-only page on back/forward; never replay mutation requests.
    const navigate = () => window.location.reload();
    window.addEventListener("popstate", navigate);
    return () => { mounted = false; actionGeneration.current += 1; window.removeEventListener("popstate", navigate); };
  }, [load]);

  const activateInnovation = (id: string) => { setActiveId(id); clearReader(); };
  const scrollToReader = useCallback(() => {
    setReaderOpen(true);
    requestAnimationFrame(() => readerRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  }, []);
  const openPaper = (page: number, quote: string) => { selectPaper({ page, quote }); setReaderTab("paper"); scrollToReader(); };
  const openCode = (path: string, start: number, end: number, why: string) => { selectCode({ path, start, end, why }); setReaderTab("code"); scrollToReader(); };
  const focusChat = useCallback(() => {
    chatRef.current?.querySelector("textarea")?.focus({ preventScroll: true });
    chatRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);
  const changeProvider = (next: ProviderConfig) => {
    const safe = providerChange(provider, next);
    if (safe !== next && provider.api_key) setNotice("端点或协议已更换，请填写新来源的密钥。");
    smokeRevision.current += 1;
    setSmoke(null);
    setProvider(safe);
  };
  const saveProvider = () => {
    try {
      window.localStorage.setItem(PROVIDER_STORAGE_KEY, JSON.stringify({ ...provider,
        api_key: rememberKey ? provider.api_key : "", remember_key: rememberKey }));
      setNotice("模型设置已保存到本机浏览器。");
    } catch { setError("浏览器无法保存设置，请检查浏览器存储权限。"); }
  };
  const changeRememberKey = (remember: boolean) => {
    setRememberKey(remember);
    if (!remember) {
      try {
        const raw = window.localStorage.getItem(PROVIDER_STORAGE_KEY);
        if (raw) window.localStorage.setItem(PROVIDER_STORAGE_KEY, JSON.stringify({ ...JSON.parse(raw), api_key: "", remember_key: false }));
      } catch { setError("无法移除浏览器已保存的密钥，请在浏览器设置中清除站点数据。"); }
    }
  };
  const changeOutputLanguage = (value: string) => {
    const language = normalizeOutputLanguage(value);
    setOutputLanguage(language);
    try { window.localStorage.setItem(OUTPUT_LANGUAGE_STORAGE_KEY, language); }
    catch { setNotice("语言已切换，但浏览器无法保存这项设置。"); }
  };
  const handleSmoke = async () => {
    const revision = ++smokeRevision.current;
    setSmokeBusy(true); setError(null);
    try { const result = await smokeTest(provider); if (revision === smokeRevision.current) setSmoke(result); }
    catch (caught) { if (revision === smokeRevision.current) setError(String(caught)); }
    finally { setSmokeBusy(false); }
  };
  const requireProvider = () => {
    if (provider.api_key.trim() && provider.model.trim()) return true;
    setSetupOpen(true); setError("请填写用于这次请求的模型名和 api_key。回看历史不需要密钥。"); return false;
  };
  const handleStart = async () => {
    if (!file) { setError("请先选择论文 PDF。"); return; }
    if (!requireProvider()) return;
    const ticket = ++actionGeneration.current;
    reset(); clearReader(); setReadOnly(false); setError(null); setNotice(null); setActiveId("");
    setSelected(new Set()); setChatScope("current"); setReaderTab("paper"); setPendingPhase("uploading");
    try {
      const created = await createRun(file, provider, outputLanguage);
      if (ticket !== actionGeneration.current) return;
      window.history.replaceState(null, "", `/?run=${encodeURIComponent(created.run_id)}`);
      const value = await load(created.run_id);
      if (!value || ticket !== actionGeneration.current) return;
      setSetupOpen(false); setPendingPhase("recon");
      await startRecon(created.run_id, provider, outputLanguage);
      if (!isCurrent(created.run_id)) return;
      await refresh(created.run_id); connect(created.run_id);
    } catch (caught) { if (ticket === actionGeneration.current) setError(String(caught)); }
    finally { if (ticket === actionGeneration.current) setPendingPhase(null); }
  };
  const handleLocate = async () => {
    if (!runId || readOnly || !requireProvider()) return;
    if (!selected.size || !repoUrl.trim()) { setError("请勾选至少一条目标并填写代码仓库地址。"); return; }
    setPendingPhase("locate"); setError(null);
    try {
      await startLocate(runId, provider, repoUrl.trim(), Array.from(selected), outputLanguage);
      if (!isCurrent(runId)) return;
      await refresh(runId); connect(runId);
    } catch (caught) { if (isCurrent(runId)) setError(String(caught)); }
    finally { if (isCurrent(runId)) setPendingPhase(null); }
  };
  const sendChat = async (message: string) => {
    if (!runId || !requireProvider()) return;
    setPendingPhase("chat"); setError(null);
    try {
      await postChat(runId, { provider, message, context_ids: chatContext ? [chatContext.id] : [], output_language: outputLanguage });
      if (!isCurrent(runId)) return;
      await refresh(runId); connect(runId);
    } catch (caught) { if (isCurrent(runId)) setError(String(caught)); }
    finally { if (isCurrent(runId)) setPendingPhase(null); }
  };
  const askAbout = (id: string) => { activateInnovation(id); setChatScope("current"); focusChat(); };
  const addTarget = async (quote: string, page: number) => {
    if (!runId || readOnly) return;
    try {
      const payload = await addPlanItem(runId, { page, quote });
      if (!isCurrent(runId)) return;
      updatePlan(runId, payload.plan); setSelected(current => new Set(current).add(payload.added.id));
      setNotice(`已加入目标「${payload.added.name}」。`);
    } catch (caught) { if (isCurrent(runId)) setError(String(caught)); }
  };
  const renameTarget = async (itemId: string, name: string) => {
    if (!runId || readOnly) return;
    try { const payload = await patchPlanItem(runId, itemId, { name }); updatePlan(runId, payload.plan); }
    catch (caught) { if (isCurrent(runId)) setError(String(caught)); }
  };
  const removeTarget = async (itemId: string) => {
    if (!runId || readOnly) return;
    try {
      const payload = await deletePlanItem(runId, itemId);
      if (!isCurrent(runId)) return;
      updatePlan(runId, payload.plan); setActiveId(""); clearReader();
      setSelected(current => new Set([...current].filter(id => id !== itemId)));
    } catch (caught) { if (isCurrent(runId)) setError(String(caught)); }
  };
  const autoSelected = useRef<string | null>(null);
  useEffect(() => {
    if (!readOnly && runId && plan?.innovations.length && autoSelected.current !== runId) {
      autoSelected.current = runId;
      queueMicrotask(() => setSelected(new Set(plan.innovations.map(item => item.id))));
    }
  }, [plan, readOnly, runId]);
  const toggle = (id: string) => setSelected(current => {
    const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next;
  });
  const toggleAll = () => setSelected(current => plan?.innovations.every(item => current.has(item.id)) ? new Set() : new Set(plan?.innovations.map(item => item.id)));
  const toggleTimeline = () => setTimelineOpen(open => {
    try { window.localStorage.setItem(TIMELINE_STORAGE_KEY, open ? "0" : "1"); }
    catch { setNotice("浏览器无法保存轨迹显示设置。"); }
    return !open;
  });
  const reconBlockers = [!file && "先选一篇 PDF", !provider.api_key && "填写 api_key", !provider.model && "填写模型名"].filter(Boolean);
  const locateBlockers = [!runId && "先上传论文", !selected.size && "勾选目标", !repoUrl.trim() && "填写仓库地址", !provider.api_key && "填写 api_key"].filter(Boolean);
  const phaseLabel = session.loading ? "正在载入历史" : chatBusy ? "正在追问" : ({ idle: "准备论文与仓库", uploading: "正在上传论文", recon: "正在侦察论文", locate: "正在定位代码", done: "本轮已结束" }[phase] ?? phase);
  const visibleError = error || session.error;

  return (
    <main className="mx-auto w-full max-w-[1560px] flex-1 px-4 py-4 sm:px-6">
      <header className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Paper2code <span className="ml-2 text-xs font-normal text-neutral-500">论文与代码，一起读懂</span></h1>
          <p className="mt-1 text-xs text-neutral-500">从核心创新点出发，阅读解释，回到原文与实现。</p>
        </div>
        <nav aria-label="主导航" className="flex flex-wrap items-center gap-4 text-sm">
          <Link href="/history" className="text-teal-800 underline-offset-4 hover:underline dark:text-teal-300">历史记录</Link>
          {readOnly && <Link href="/" onClick={() => { reset(); setReadOnly(false); setSetupOpen(true); clearReader(); setSelected(new Set()); setActiveId(""); }} className="text-teal-800 underline-offset-4 hover:underline dark:text-teal-300">新建解读</Link>}
          <span className="text-xs text-neutral-500" title={`后端 ${API_BASE}`}>{backendUp === null ? "连接检查中…" : backendUp ? "服务已连接" : "服务未连接，请启动后端"}</span>
        </nav>
      </header>

      <div className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-2">
        <label htmlFor="output-language" className="text-sm font-medium">解析语言</label>
        <select id="output-language" value={outputLanguage} onChange={(event) => changeOutputLanguage(event.target.value)} disabled={busy || chatBusy} aria-describedby="output-language-hint" className="min-h-10 rounded-lg border border-neutral-300 bg-white px-3 py-2 text-base focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-600 disabled:cursor-wait disabled:opacity-60 dark:border-neutral-700 dark:bg-neutral-950">
          <option value="zh-CN">简体中文</option>
          <option value="en">English</option>
        </select>
        <p id="output-language-hint" className="text-sm text-neutral-600 dark:text-neutral-400">用于新生成的创新点、代码讲解和追问；已有结果与引用原文保持不变。</p>
      </div>

      {visibleError && <div role="alert" className="sticky top-2 z-50 mb-4 flex items-start gap-2 rounded-lg border border-red-300 bg-red-50 p-3 text-sm shadow-lg dark:border-red-900 dark:bg-red-950 dark:text-red-100">
        <span className="min-w-0 flex-1 break-words">{visibleError}</span>
        <button type="button" onClick={() => setError(null)} className="shrink-0 text-xs underline">关闭</button>
      </div>}
      {notice && <div role="status" className="sticky top-2 z-40 mb-4 flex items-start gap-2 rounded-lg border border-blue-300 bg-blue-50 p-3 text-sm dark:border-blue-900 dark:bg-blue-950 dark:text-blue-100">
        <span className="min-w-0 flex-1">{notice}</span>
        <button type="button" onClick={() => setNotice(null)} className="shrink-0 text-xs underline">知道了</button>
      </div>}

      {readOnly && snapshot && <section aria-label="历史解读信息" className="mb-4 border-y border-neutral-200 py-4 dark:border-neutral-800">
        <h2 className="break-words text-lg font-semibold">{snapshot.history.title}</h2>
        <p className="mt-2 text-sm text-neutral-600 dark:text-neutral-400">{HISTORY_STATUS[snapshot.history.analysis_status] || snapshot.history.analysis_status} · 原模型：{snapshot.history.original_provider?.model || "未记录"}。已有创新点与结论保留，后续追问使用下方设置的模型。</p>
        {operation?.status === "interrupted" && <p role="status" className="mt-2 text-sm text-amber-800 dark:text-amber-300">{operation.reason}</p>}
      </section>}
      {snapshot?.warnings.map(warning => <p role="status" key={warning} className="mb-3 break-words text-sm text-amber-800 dark:text-amber-300">{warning}</p>)}
      {session.loading && <p role="status" className="mb-4 text-sm">正在载入已保存的解读…</p>}

      <div className="run-status sticky top-0 z-30 mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-neutral-200 bg-white/95 px-4 py-3 backdrop-blur dark:border-neutral-800 dark:bg-neutral-950/95">
        <span className={`h-2 w-2 rounded-full ${busy ? "animate-pulse bg-teal-500" : visibleError ? "bg-red-500" : "bg-neutral-400"}`} />
        <span role="status" className="text-sm font-medium">{phaseLabel}</span>
        {plan && <span className="text-xs text-neutral-500">{plan.innovations.length} 条创新点 · {findings.length} 条结论</span>}
        {runId && <nav aria-label="阅读快捷入口" className="flex gap-3 text-xs text-teal-700 dark:text-teal-300"><button type="button" onClick={scrollToReader}>原文与代码</button><button type="button" onClick={focusChat}>追问</button></nav>}
        <button type="button" onClick={toggleTimeline} aria-expanded={timelineOpen} aria-controls="run-timeline run-timeline-sidebar" className="ml-auto text-xs text-neutral-500">{timelineOpen ? "收起轨迹" : "展开行动轨迹"} · {events.length}</button>
      </div>

      <div className="flex items-start gap-5">
        <div className="min-w-0 flex-1 space-y-4">
          <section className="setup-panel rounded-xl border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-950">
            <button type="button" onClick={() => setSetupOpen(!setupOpen)} aria-expanded={setupOpen} aria-controls="analysis-setup" className="flex w-full flex-wrap items-center gap-2 px-4 py-3 text-left">
              <span className="text-sm font-semibold">{readOnly ? "后续追问的模型" : "分析准备"}</span>
              <span className="min-w-0 flex-1 truncate text-xs text-neutral-500">{paper ? `${paper.filename} · ${paper.page_count} 页` : "选择模型、上传论文"}{provider.model ? ` · ${provider.model}` : ""}</span>
              <span className="text-xs text-teal-700 dark:text-teal-300">{setupOpen ? "收起 ↑" : readOnly ? "更换来源 / 模型 ↓" : "修改设置 / 更换论文 ↓"}</span>
            </button>
            <div id="analysis-setup" hidden={!setupOpen} className="border-t border-neutral-200 p-4 dark:border-neutral-800">
              <div className="grid items-start gap-5 md:grid-cols-2">
                <div className="min-w-0">
                  <ProviderForm value={provider} onChange={changeProvider} smoke={smoke} smokeBusy={smokeBusy} onSmoke={handleSmoke} onSave={saveProvider} />
                  <label className="mt-3 flex items-start gap-2 text-xs text-neutral-500"><input type="checkbox" checked={rememberKey} onChange={(event) => changeRememberKey(event.target.checked)} />把 api_key 存在这个浏览器里（默认不存；存了就等于交给 localStorage）</label>
                </div>
                {readOnly ? <p className="text-sm leading-7 text-neutral-600 dark:text-neutral-400">回看历史无需配置模型。要继续追问时，选择可用的来源、填写模型名和密钥即可；新模型会读取已有解读和对话。更换端点或协议后需要重新填写密钥。</p> : <section className="min-w-0">
                  <h2 className="mb-3 text-sm font-semibold">论文 PDF</h2>
                  <input type="file" aria-label="论文 PDF" accept="application/pdf" onChange={(event) => setFile(event.target.files?.[0] ?? null)} className="block w-full text-xs file:mr-3 file:rounded file:border-0 file:bg-neutral-100 file:px-3 file:py-2 dark:file:bg-neutral-800" />
                  {paper && <p className="mt-2 break-words text-xs text-neutral-500">{paper.filename} · {paper.page_count} 页</p>}
                  <button type="button" onClick={handleStart} disabled={busy} className="primary-button mt-4 w-full">{phase === "uploading" ? "上传中…" : phase === "recon" ? "正在侦察论文…" : "上传并开始侦察"}</button>
                  {reconBlockers.length > 0 && <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">还差：{reconBlockers.join("、")}</p>}
                  <p className="mt-3 text-xs leading-6 text-neutral-500">先梳理论文的核心创新点，再选择要定位的实现。每条论文引用都会核对页码与原文。</p>
                  {paperReady && !runEnd && <p className="mt-2 text-xs text-neutral-500">论文已解析完成</p>}
                </section>}
              </div>
            </div>
          </section>

          {!readOnly && <section className="rounded-xl border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-950">
            <label htmlFor="repository" className="mb-2 block text-sm font-semibold">代码仓库</label>
            <div className="flex flex-col gap-2 sm:flex-row">
              <input id="repository" value={repoUrl} onChange={(event) => setRepoUrl(event.target.value)} placeholder="https://github.com/owner/repo" className="min-w-0 flex-1 rounded-lg border border-neutral-300 bg-transparent px-3 py-2 font-mono text-xs dark:border-neutral-700" />
              <button type="button" onClick={handleLocate} disabled={busy || locateBusy} className="primary-button shrink-0">{phase === "locate" ? "正在定位…" : `开始定位选中的 ${selected.size} 条`}</button>
            </div>
            {locateBlockers.length > 0 && <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">还差：{locateBlockers.join("、")}</p>}
          </section>}

          {snapshot?.has_older_events && <button type="button" onClick={() => void session.older()} className="min-h-10 text-sm text-teal-800 underline dark:text-teal-300">加载更早的行动轨迹</button>}

          <div id="run-timeline" className="xl:hidden" hidden={!timelineOpen}><Timeline events={events} variant="inline" /></div>

          <section className="innovation-workspace rounded-xl border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-950" aria-label="创新点阅读区">
            <header className="flex flex-wrap items-center gap-3 border-b border-neutral-200 px-5 py-4 dark:border-neutral-800">
              <h2 className="text-sm font-semibold">核心创新点 {plan && <span className="ml-1 font-normal text-neutral-500">{plan.innovations.length} 条</span>}</h2>
              <div className="ml-auto flex gap-4 text-xs text-teal-700 dark:text-teal-300">
                {runId && <button type="button" onClick={() => openPaper(1, "")}>{readOnly ? "浏览历史论文 ↗" : "浏览论文 / 划选目标 ↗"}</button>}
                {runId && <button type="button" onClick={focusChat}>追问 ↗</button>}
              </div>
            </header>
            {plan ? <>
              <details className="paper-summary border-b border-neutral-200 px-5 py-3 text-sm dark:border-neutral-800">
                <summary className="text-xs text-neutral-500">论文摘要与覆盖说明</summary>
                <Markdown text={plan.paper_summary} className="mt-3 leading-7" />
                {plan.coverage_note && <p className="mt-2 text-xs text-neutral-500">覆盖说明：<Markdown inline text={plan.coverage_note} /></p>}
              </details>
              <div className="innovation-layout">
                <PlanList key={runId} readOnly={readOnly} plan={plan} selected={selected} activeId={activeInnovation?.id ?? ""} findings={findings} missingIds={missingIds} busy={busy} onActivate={activateInnovation} onToggle={toggle} onSelectAll={toggleAll} />
                {activeInnovation && <div id="innovation-detail" role="tabpanel" aria-labelledby={`innovation-tab-${activeInnovation.id}`} tabIndex={0} className="innovation-detail">
                  <PlanDetail key={`${runId}-${activeInnovation.id}`} readOnly={readOnly} active={activeInnovation} hasFinding={Boolean(activeFinding)} onRename={renameTarget} onDelete={removeTarget} onOpenPaper={openPaper} />
                  <ComparePanel readOnly={readOnly} key={`finding-${activeInnovation.id}`} finding={activeFinding} missing={missingIds.includes(activeInnovation.id)} busy={phase === "locate"} selected={selected.has(activeInnovation.id)} onOpenPaper={openPaper} onOpenCode={openCode} onAsk={askAbout} />
                </div>}
              </div>
            </> : <p className="px-5 py-8 text-sm text-neutral-500">{busy ? "正在阅读论文，创新点清单将在这里出现。" : readOnly ? "这份历史尚未保存创新点清单，仍可浏览论文或继续追问。" : "上传论文并开始侦察，在这里逐条阅读创新点及对应实现。"}</p>}
          </section>

          <section ref={readerRef} className="scroll-mt-24" aria-label="对照阅读器">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-sm font-semibold">对照阅读器 <span className="ml-2 text-xs font-normal text-neutral-500">{activeInnovation ? <>当前：<Markdown inline text={activeInnovation.name} /></> : "原文与代码"}</span></h2>
              {runId && <button type="button" onClick={() => setReaderOpen(!readerOpen)} aria-expanded={readerOpen} aria-controls="reader-content" className="min-h-9 shrink-0 rounded px-2 text-xs text-teal-700 hover:bg-teal-50 dark:text-teal-300 dark:hover:bg-teal-950">{readerOpen ? "收起阅读器 ↑" : "展开阅读器 ↓"}</button>}
            </div>
            <div id="reader-content" hidden={!readerOpen}>
              {runId ? <Reader left={paperPane} right={codePane} walkthrough={activeFinding?.explanation?.code_walkthrough} onOpenCode={openCode} activeTab={readerTab} onTabChange={setReaderTab} codeEmpty={phase === "locate" ? "正在定位代码，找到实现后会自动显示。" : activeFinding ? "这条创新点暂无可展示的代码引用。" : readOnly ? "历史中暂无代码引用，可继续追问已有内容。" : "开始定位后，这里会自动显示当前创新点的代码。"} pdfUrl={pdfUrl(runId)} pageImageUrl={(page: number) => paperPageImageUrl(runId, page)} onGoToPage={(page) => selectPaper({ page, quote: "" })} onSelectTarget={readOnly ? undefined : addTarget} /> : <p className="text-xs text-neutral-500">上传论文后，在这里对照原文与实现。</p>}
            </div>
          </section>

          <div ref={chatRef} hidden={!runId} className="scroll-mt-24">
            <ChatPanel key={runId} turns={chatTurns} streaming={chatStreaming} busy={chatBusy} progress={pendingPhase === "chat" ? "正在发送问题" : chatProgress.status} stoppedReason={chatProgress.reason} context={chatContext} onClearContext={() => setChatScope(chatScope === "current" ? "all" : "current")} onSend={sendChat} disabled={!runId || busy || snapshot?.can_chat === false} onOpenCitation={(path, start, end) => openCode(path, start, end, "来自追问对话")} />
          </div>

          {runEnd && <details className="rounded-lg border border-neutral-200 dark:border-neutral-800">
            <summary className="px-4 py-3 text-xs text-neutral-500">覆盖率与预算{verification ? ` · 引用核验率 ${(verification.citation_verifiable_rate * 100).toFixed(0)}% · ${verification.citations_verified}/${verification.citations_total} 条` : ""}{missingIds.length ? ` · ${missingIds.length} 条未产出结论` : ""}</summary>
            <CoverageCard events={events} verification={verification} missingIds={missingIds} filesTotal={filesTotal} />
            {commitSha && <p className="break-all px-4 pb-3 font-mono text-[11px] text-neutral-500">commit {commitSha}</p>}
          </details>}
        </div>

        <aside id="run-timeline-sidebar" hidden={!timelineOpen} className={`timeline-sidebar sticky top-20 h-[calc(100vh-6rem)] w-52 shrink-0 ${timelineOpen ? "hidden xl:block" : "hidden"}`} aria-label="Agent 行动轨迹">
          <h2 className="mb-3 text-xs font-semibold text-neutral-500">Agent 行动轨迹（实时）</h2>
          <div className="h-[calc(100%-2rem)]"><Timeline events={events} variant="sidebar" /></div>
        </aside>
      </div>
    </main>
  );
}
