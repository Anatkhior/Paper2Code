"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { chatState } from "@/lib/chatState";
import { ACTIVE_RUN_SESSION_KEY, migrateStorage, PROVIDER_STORAGE_KEY, TIMELINE_STORAGE_KEY } from "@/lib/providerStorage";
import { DEFAULT_OUTPUT_LANGUAGE, normalizeOutputLanguage, OUTPUT_LANGUAGE_STORAGE_KEY } from "@/lib/language";
import { HISTORY_STATUS, providerChange, statusTone } from "@/lib/session";
import { useRunSession } from "@/lib/useRunSession";
import ChatPanel from "@/components/ChatPanel";
import Markdown from "@/components/Markdown";
import ComparePanel from "@/components/ComparePanel";
import Icon from "@/components/Icon";
import CoverageCard from "@/components/CoverageCard";
import PlanList, { PlanDetail } from "@/components/PlanList";
import ProviderForm from "@/components/ProviderForm";
import Reader from "@/components/Reader";
import ThemeToggle from "@/components/ThemeToggle";
import { useReader } from "@/lib/useReader";
import Timeline from "@/components/Timeline";
import { addPlanItem, API_BASE, cancelRun, createRun, deletePlanItem, healthUrl, paperPageImageUrl,
  patchPlanItem, pdfUrl, postChat, smokeTest, startLocate, startRecon } from "@/lib/api";
import type { OutputLanguage, ProviderConfig, SmokeResult } from "@/lib/types";

const EMPTY_PROVIDER: ProviderConfig = { protocol: "openai-compatible", base_url: "", api_key: "", model: "" };
const EMPTY_EVENTS: import("@/lib/types").RunEvent[] = [];
const DONE_LABEL: Record<string, string> = { ok: "本轮已结束", failed: "本轮失败，已保存进展", cancelled: "已停止", interrupted: "任务已中断" };
const STEPS = [
  { title: "配置模型", text: "填写来源、模型名和 api_key，先跑一次自检，确认它能可靠地调用工具。" },
  { title: "上传论文", text: "Agent 通读 PDF，梳理出核心创新点；每条引文都会核对页码与原文。" },
  { title: "定位代码", text: "填入代码仓库，勾选创新点后定位实现，再并排对照阅读、继续追问。" },
];

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
  const [dragging, setDragging] = useState(false);
  const [cancelling, setCancelling] = useState(false);
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
  const runEnd = useMemo(() => events.findLast(event => event.type === "run_end")?.data, [events]);
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
      if (id) {
        let sameTabActive = false;
        try { sameTabActive = window.sessionStorage.getItem(ACTIVE_RUN_SESSION_KEY) === id; } catch { /* ignore */ }
        setReadOnly(!sameTabActive);
        setSetupOpen(false);
        void load(id);
      }
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
      try { window.sessionStorage.setItem(ACTIVE_RUN_SESSION_KEY, created.run_id); } catch { /* ignore */ }
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
  const handleCancel = async () => {
    if (!runId || cancelling) return;
    setCancelling(true);
    try {
      const result = await cancelRun(runId);
      if (!isCurrent(runId)) return;
      if (!result.cancelled) setNotice("任务已经结束，无需停止。");
      await refresh(runId);
    } catch (caught) { if (isCurrent(runId)) setError(String(caught)); }
    finally { setCancelling(false); }
  };
  const pickFile = (next: File | null) => {
    if (next && next.type !== "application/pdf" && !next.name.toLowerCase().endsWith(".pdf")) { setError("请选择 PDF 文件。"); return; }
    setFile(next);
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
  const locateBlockers = [!runId && "先上传论文", !selected.size && "勾选目标", !repoUrl.trim() && "填写仓库地址", !provider.api_key && "填写 api_key", !provider.model.trim() && "填写模型名"].filter(Boolean);
  const innovations = plan?.innovations ?? [];
  const activeIndex = activeInnovation ? innovations.findIndex(item => item.id === activeInnovation.id) : -1;
  const stepInnovation = (delta: number) => {
    if (innovations.length < 2 || activeIndex < 0) return;
    const next = (activeIndex + delta + innovations.length) % innovations.length;
    activateInnovation(innovations[next].id);
  };
  const startNewRun = () => {
    try { window.sessionStorage.removeItem(ACTIVE_RUN_SESSION_KEY); } catch { /* ignore */ }
    reset(); setReadOnly(false); setSetupOpen(true); clearReader(); setSelected(new Set()); setActiveId(""); setFile(null); setError(null); setNotice(null);
  };
  const resumeIncompleteRun = () => {
    if (!runId) return;
    try { window.sessionStorage.setItem(ACTIVE_RUN_SESSION_KEY, runId); } catch { /* ignore */ }
    setReadOnly(false);
    setNotice("已切换为可编辑模式：现在可以调整创新点目标并填写仓库继续定位。");
  };
  const doneStatus = phase === "done" ? operation?.status : undefined;
  const phaseLabel = session.loading ? "正在载入历史" : chatBusy ? "正在追问" : phase === "done" ? operation ? DONE_LABEL[operation.status] ?? "本轮已结束" : "论文已上传" : ({ idle: "准备论文与仓库", uploading: "正在上传论文", recon: "正在侦察论文", locate: "正在定位代码" }[phase] ?? phase);
  const visibleError = error || session.error;
  const runTone = busy || chatBusy ? "run" : visibleError ? "fail" : doneStatus && doneStatus !== "running" ? statusTone(doneStatus) : "muted";
  const endReason = doneStatus && doneStatus !== "ok" && doneStatus !== "running" ? operation?.reason : undefined;
  const olderButton = snapshot?.has_older_events && <button type="button" onClick={() => void session.older()} className="quiet-button mb-2">加载更早的行动轨迹</button>;

  return (
    <main className="mx-auto w-full max-w-[1560px] flex-1 px-4 py-4 sm:px-6 sm:py-5">
      <header className="mb-4 flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-tight">Paper2code <span className="ml-2 text-sm font-normal text-muted">论文与代码，一起读懂</span></h1>
          <p className="mt-1 text-xs text-muted">从核心创新点出发，阅读解释，回到原文与实现。</p>
        </div>
        <nav aria-label="主导航" className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
          <span className="flex items-center gap-2">
            <label htmlFor="output-language" className="text-xs text-muted">解析语言</label>
            <select id="output-language" value={outputLanguage} onChange={(event) => changeOutputLanguage(event.target.value)} disabled={busy || chatBusy} aria-describedby="output-language-hint" title="用于新生成的创新点、代码讲解和追问；已有结果与引用原文保持不变。" className="field field-compact">
              <option value="zh-CN">简体中文</option>
              <option value="en">English</option>
            </select>
            <span id="output-language-hint" className="sr-only">用于新生成的创新点、代码讲解和追问；已有结果与引用原文保持不变。</span>
          </span>
          <Link href="/history" className="text-link">历史记录</Link>
          {runId && <Link href="/" onClick={startNewRun} className="text-link">新建解读</Link>}
          <ThemeToggle />
          <span className="flex items-center gap-2 text-xs text-muted" data-tone={backendUp === null ? "muted" : backendUp ? "ok" : "fail"} title={`后端 ${API_BASE}`}>
            <span className="status-dot" />{backendUp === null ? "连接检查中…" : backendUp ? "服务已连接" : "服务未连接，请启动后端"}
          </span>
        </nav>
      </header>

      {visibleError && <div role="alert" className="alert-fail sticky top-2 z-50 mb-4 flex items-start gap-3 rounded-lg px-4 py-3 text-sm shadow-[0_6px_20px_-8px_rgb(0_0_0/0.25)]">
        <Icon name="alert" size={16} className="mt-0.5 shrink-0 text-fail" />
        <span className="min-w-0 flex-1 break-words leading-6">{visibleError}</span>
        <button type="button" onClick={() => setError(null)} className="shrink-0 rounded px-1 text-xs underline underline-offset-4">关闭</button>
      </div>}
      {notice && <div role="status" className="sticky top-2 z-40 mb-4 flex items-start gap-3 rounded-lg border border-accent-line bg-accent-soft px-4 py-3 text-sm shadow-[0_6px_20px_-10px_rgb(0_0_0/0.2)]">
        <Icon name="info" size={16} className="mt-0.5 shrink-0 text-accent" />
        <span className="min-w-0 flex-1 leading-6">{notice}</span>
        <button type="button" onClick={() => setNotice(null)} className="shrink-0 rounded px-1 text-xs text-accent underline underline-offset-4">知道了</button>
      </div>}

      {readOnly && snapshot && <section aria-label="历史解读信息" className="mb-4 border-b border-line pb-4">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <h2 className="min-w-0 break-words text-lg font-semibold tracking-tight">{snapshot.history.title}</h2>
          <span data-tone={statusTone(snapshot.history.analysis_status)} className="badge">{HISTORY_STATUS[snapshot.history.analysis_status] || snapshot.history.analysis_status}</span>
          {!snapshot.artifact && !snapshot.active && snapshot.resources.paper && (
            <button type="button" onClick={resumeIncompleteRun} className="secondary-button ml-auto min-h-8 px-3 py-1 text-xs">继续完善此解读</button>
          )}
        </div>
        <p className="mt-1.5 text-sm leading-6 text-muted">原模型：{snapshot.history.original_provider?.model || "未记录"}。已有创新点与结论保留，后续追问使用下方设置的模型。</p>
      </section>}
      {snapshot?.warnings.map(warning => <p role="status" key={warning} className="mb-3 break-words text-sm text-warn">{warning}</p>)}
      {session.loading && <p role="status" className="mb-4 text-sm text-muted">正在载入已保存的解读…</p>}

      <div data-tone={runTone} className="run-status sticky top-0 z-30 mb-4 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-line bg-surface/95 px-4 py-2.5 shadow-[0_4px_16px_-12px_rgb(0_0_0/0.35)] backdrop-blur">
        <span className="flex min-w-0 items-center gap-2.5">
          <span className="status-dot" />
          <span role="status" className="text-sm font-medium" style={runTone === "muted" ? undefined : { color: "var(--tone)" }}>{phaseLabel}</span>
        </span>
        {endReason && <span className="min-w-0 max-w-[48ch] truncate text-xs text-muted" title={endReason}>{endReason}</span>}
        {runId && snapshot?.active && <button type="button" onClick={handleCancel} disabled={cancelling} className="danger-button"><Icon name="stop" size={12} />{cancelling ? "正在停止…" : "停止"}</button>}
        {plan && <span className="text-xs tabular-nums text-muted">{plan.innovations.length} 条创新点 · {findings.length} 条结论</span>}
        {runId && <nav aria-label="阅读快捷入口" className="flex gap-1"><button type="button" onClick={scrollToReader} className="quiet-button">原文与代码</button><button type="button" onClick={focusChat} className="quiet-button">追问</button></nav>}
        <button type="button" onClick={toggleTimeline} aria-expanded={timelineOpen} aria-controls="run-timeline run-timeline-sidebar" className="quiet-button ml-auto text-muted">{timelineOpen ? "收起轨迹" : "展开行动轨迹"} · <span className="tabular-nums">{events.length}</span></button>
      </div>

      <div className="flex items-start gap-5">
        <div className="min-w-0 flex-1 space-y-4">
          <section className="setup-panel panel">
            <button type="button" onClick={() => setSetupOpen(!setupOpen)} aria-expanded={setupOpen} aria-controls="analysis-setup" className="flex min-h-12 w-full flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 text-left">
              <span className="text-sm font-semibold">{readOnly ? "后续追问的模型" : "分析准备"}</span>
              <span className="min-w-0 flex-1 truncate text-xs text-muted">{paper ? `${paper.filename} · ${paper.page_count} 页` : "选择模型、上传论文"}{provider.model ? ` · ${provider.model}` : ""}</span>
              <span className="flex items-center gap-1 text-xs text-accent">{setupOpen ? "收起" : readOnly ? "更换来源 / 模型" : "修改设置 / 更换论文"}<Icon name="chevron" size={14} className={setupOpen ? "rotate-180" : ""} /></span>
            </button>
            <div id="analysis-setup" hidden={!setupOpen} className="border-t border-line">
              <div className="grid items-start md:grid-cols-2">
                <div className="min-w-0 p-4 sm:p-5">
                  <ProviderForm value={provider} onChange={changeProvider} smoke={smoke} smokeBusy={smokeBusy} onSmoke={handleSmoke} onSave={saveProvider} />
                  <label className="mt-4 flex items-start gap-2 text-xs leading-5 text-muted"><input type="checkbox" checked={rememberKey} onChange={(event) => changeRememberKey(event.target.checked)} className="mt-1 shrink-0" />把 api_key 存在这个浏览器里（默认不存；存了就等于交给 localStorage）</label>
                </div>
                {readOnly ? <p className="border-t border-line p-4 text-sm leading-7 text-muted sm:p-5 md:border-l md:border-t-0">回看历史无需配置模型。要继续追问时，选择可用的来源、填写模型名和密钥即可；新模型会读取已有解读和对话。更换端点或协议后需要重新填写密钥。</p> : <section className="min-w-0 border-t border-line p-4 sm:p-5 md:self-stretch md:border-l md:border-t-0">
                  <h2 className="mb-4 text-sm font-semibold">论文 PDF</h2>
                  <label className="file-picker" data-filled={file ? "" : undefined} data-dragging={dragging ? "" : undefined}
                    onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
                    onDragLeave={() => setDragging(false)}
                    onDrop={(event) => { event.preventDefault(); setDragging(false); pickFile(event.dataTransfer.files?.[0] ?? null); }}>
                    <input type="file" aria-label="论文 PDF" accept="application/pdf" onChange={(event) => pickFile(event.target.files?.[0] ?? null)} className="sr-only" />
                    <span className="grid size-10 shrink-0 place-items-center rounded-lg border border-line bg-surface text-accent"><Icon name={file ? "paper" : "upload"} size={18} /></span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium">{file ? file.name : "选择论文 PDF"}</span>
                      <span className="mt-0.5 block text-xs text-muted">{file ? `${(file.size / 1024 / 1024).toFixed(1)} MB · 点击更换` : "点击选择，或把 PDF 拖到这里"}</span>
                    </span>
                  </label>
                  {paper && <p className="mt-2 break-words text-xs text-muted">当前解读：{paper.filename} · {paper.page_count} 页</p>}
                  <button type="button" onClick={handleStart} disabled={busy} className="primary-button mt-4 w-full">{phase === "uploading" ? "上传中…" : phase === "recon" ? "正在侦察论文…" : "上传并开始侦察"}</button>
                  {reconBlockers.length > 0 && <p className="mt-2 text-xs text-warn">还差：{reconBlockers.join("、")}</p>}
                  <p className="mt-3 text-xs leading-6 text-muted">先梳理论文的核心创新点，再选择要定位的实现。每条论文引用都会核对页码与原文。</p>
                  {paperReady && !runEnd && <p className="mt-2 text-xs text-muted">论文已解析完成</p>}
                </section>}
              </div>
            </div>
          </section>

          {!readOnly && <section className="panel p-4 sm:px-5">
            <label htmlFor="repository" className="mb-2 block text-sm font-semibold">代码仓库</label>
            <div className="flex flex-col gap-2 sm:flex-row">
              <input id="repository" value={repoUrl} onChange={(event) => setRepoUrl(event.target.value)} placeholder="https://github.com/owner/repo" spellCheck={false} className="field flex-1 font-mono" />
              <button type="button" onClick={handleLocate} disabled={busy || locateBusy} className="primary-button shrink-0">{phase === "locate" ? "正在定位…" : `开始定位选中的 ${selected.size} 条`}</button>
            </div>
            {locateBlockers.length > 0 && <p className="mt-2 text-xs text-warn">还差：{locateBlockers.join("、")}</p>}
          </section>}

          <section className="innovation-workspace panel overflow-hidden" aria-label="创新点阅读区">
            <header className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-4 py-3 sm:px-5">
              <h2 className="text-base font-semibold tracking-tight">核心创新点 {plan && <span className="ml-1 text-sm font-normal tabular-nums text-muted">{plan.innovations.length} 条</span>}</h2>
              <div className="ml-auto flex flex-wrap gap-1">
                {runId && <button type="button" onClick={() => openPaper(1, "")} className="quiet-button">{readOnly ? "浏览历史论文 ↗" : "浏览论文 / 划选目标 ↗"}</button>}
                {runId && <button type="button" onClick={focusChat} className="quiet-button">追问 ↗</button>}
              </div>
            </header>
            {plan ? <>
              <details className="paper-summary border-b border-line px-4 py-3 text-sm sm:px-5">
                <summary className="w-fit text-xs font-medium text-muted">论文摘要与覆盖说明</summary>
                <Markdown text={plan.paper_summary} className="mt-3 max-w-[78ch] leading-7" />
                {plan.coverage_note && <p className="mt-2 text-xs text-muted">覆盖说明：<Markdown inline text={plan.coverage_note} /></p>}
              </details>
              <div className="innovation-layout">
                <PlanList key={runId} readOnly={readOnly} plan={plan} selected={selected} activeId={activeInnovation?.id ?? ""} findings={findings} missingIds={missingIds} busy={busy} onActivate={activateInnovation} onToggle={toggle} onSelectAll={toggleAll} />
                {activeInnovation && <div id="innovation-detail" role="tabpanel" aria-labelledby={`innovation-tab-${activeInnovation.id}`} tabIndex={0} className="innovation-detail">
                  <PlanDetail key={`${runId}-${activeInnovation.id}`} readOnly={readOnly} active={activeInnovation} hasFinding={Boolean(activeFinding)} onRename={renameTarget} onDelete={removeTarget} onOpenPaper={openPaper} />
                  <ComparePanel readOnly={readOnly} key={`finding-${activeInnovation.id}`} finding={activeFinding} missing={missingIds.includes(activeInnovation.id)} busy={phase === "locate"} selected={selected.has(activeInnovation.id)} onOpenPaper={openPaper} onOpenCode={openCode} onAsk={askAbout} />
                </div>}
              </div>
            </> : busy || readOnly ? <p data-tone={busy ? "run" : "muted"} className="flex items-center gap-2.5 px-5 py-10 text-sm text-muted">{busy && <span className="status-dot" />}{busy ? "正在阅读论文，创新点清单将在这里出现。" : "这份历史尚未保存创新点清单，仍可浏览论文或继续追问。"}</p>
              : runId ? <p className="px-5 py-10 text-sm leading-6 text-muted">这次没有产出创新点清单。查看行动轨迹里的结束原因，或修改设置后重新上传侦察。</p>
              : <ol className="onboarding-steps" aria-label="使用步骤">
                {STEPS.map((step, index) => <li key={step.title}>
                  <span className="step-index">{index + 1}</span>
                  <div className="min-w-0"><h3 className="text-sm font-semibold">{step.title}</h3><p className="mt-1 text-sm leading-6 text-muted">{step.text}</p></div>
                </li>)}
              </ol>}
          </section>

          <div id="run-timeline" className="xl:hidden" hidden={!timelineOpen}>
            <h2 className="mb-2 text-xs font-semibold text-muted">Agent 行动轨迹</h2>
            {olderButton}
            <Timeline events={events} variant="inline" />
          </div>

          <section ref={readerRef} className="scroll-mt-24" aria-label="对照阅读器">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h2 className="min-w-0 text-base font-semibold tracking-tight">对照阅读器 <span className="ml-2 text-xs font-normal text-muted">{activeInnovation ? <>当前：{activeIndex >= 0 ? `${activeIndex + 1}/${innovations.length} · ` : ""}<Markdown inline text={activeInnovation.name} /></> : "原文与代码"}</span></h2>
              <div className="flex flex-wrap items-center gap-1">
                {innovations.length > 1 && <>
                  <button type="button" onClick={() => stepInnovation(-1)} className="quiet-button text-muted" aria-label="切换上一条创新点"><Icon name="arrowLeft" size={14} />上一条</button>
                  <button type="button" onClick={() => stepInnovation(1)} className="quiet-button text-muted" aria-label="切换下一条创新点">下一条<Icon name="arrowRight" size={14} /></button>
                </>}
                {runId && <button type="button" onClick={() => setReaderOpen(!readerOpen)} aria-expanded={readerOpen} aria-controls="reader-content" className="quiet-button">{readerOpen ? "收起阅读器" : "展开阅读器"}<Icon name="chevron" size={14} className={readerOpen ? "rotate-180" : ""} /></button>}
              </div>
            </div>
            <div id="reader-content" hidden={!readerOpen}>
              {runId ? <Reader left={paperPane} right={codePane} walkthrough={activeFinding?.explanation?.code_walkthrough} onOpenCode={openCode} activeTab={readerTab} onTabChange={setReaderTab} codeEmpty={phase === "locate" ? "正在定位代码，找到实现后会自动显示。" : activeFinding ? "这条创新点暂无可展示的代码引用。" : readOnly ? "历史中暂无代码引用，可继续追问已有内容。" : "开始定位后，这里会自动显示当前创新点的代码。"} pdfUrl={pdfUrl(runId)} pageImageUrl={(page: number) => paperPageImageUrl(runId, page)} onGoToPage={(page) => selectPaper({ page, quote: "" })} onSelectTarget={readOnly ? undefined : addTarget} /> : <p className="rounded-xl border border-dashed border-line-strong px-4 py-6 text-sm text-muted">上传论文后，在这里并排对照原文与实现。</p>}
            </div>
          </section>

          <div ref={chatRef} hidden={!runId} className="scroll-mt-24">
            <ChatPanel key={runId} turns={chatTurns} streaming={chatStreaming} busy={chatBusy} progress={pendingPhase === "chat" ? "正在发送问题" : chatProgress.status} incomplete={chatProgress.incomplete} stoppedReason={chatProgress.reason} context={chatContext} canFocusInnovation={Boolean(activeInnovation)} onClearContext={() => setChatScope(chatScope === "current" ? "all" : "current")} onSend={sendChat} disabled={!runId || busy || snapshot?.can_chat === false} onOpenCitation={(path, start, end) => openCode(path, start, end, "来自追问对话")} />
          </div>

          {runEnd && <details className="panel">
            <summary className="px-4 py-3 text-xs text-muted">覆盖率与预算{verification ? ` · 引用核验率 ${(verification.citation_verifiable_rate * 100).toFixed(0)}% · ${verification.citations_verified}/${verification.citations_total} 条` : ""}{missingIds.length ? ` · ${missingIds.length} 条未产出结论` : ""}</summary>
            <div className="border-t border-line p-1"><CoverageCard events={events} verification={verification} missingIds={missingIds} filesTotal={filesTotal} /></div>
            {commitSha && <p className="break-all px-4 pb-3 font-mono text-[11px] text-muted">commit {commitSha}</p>}
          </details>}
        </div>

        <aside id="run-timeline-sidebar" hidden={!timelineOpen} className={`timeline-sidebar sticky top-20 h-[calc(100vh-6rem)] w-64 shrink-0 ${timelineOpen ? "hidden xl:block" : "hidden"}`} aria-label="Agent 行动轨迹">
          <div className="flex h-full flex-col">
            <h2 className="mb-2 text-xs font-semibold text-muted">Agent 行动轨迹（实时）</h2>
            {olderButton}
            <div className="min-h-0 flex-1"><Timeline events={events} variant="sidebar" /></div>
          </div>
        </aside>
      </div>
    </main>
  );
}
