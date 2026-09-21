"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import ChatPanel, { type ChatTurn } from "@/components/ChatPanel";
import ComparePanel from "@/components/ComparePanel";
import CoverageCard from "@/components/CoverageCard";
import PlanList, { PlanDetail } from "@/components/PlanList";
import ProviderForm from "@/components/ProviderForm";
import Reader from "@/components/Reader";
import { useReader } from "@/lib/useReader";
import Timeline from "@/components/Timeline";
import {
  addPlanItem,
  API_BASE,
  createRun,
  deletePlanItem,
  eventsUrl,
  fetchChat,
  fetchPlan,
  healthUrl,
  paperPageImageUrl,
  patchPlanItem,
  pdfUrl,
  postChat,
  smokeTest,
  startLocate,
  startRecon,
} from "@/lib/api";
import {
  EVENT_TYPES,
  type Finding,
  type PaperMeta,
  type Plan,
  type ProviderConfig,
  type RunEvent,
  type SmokeResult,
  type VerificationSummary,
} from "@/lib/types";

const STORAGE_KEY = "paperlens.provider.v1";

const EMPTY_PROVIDER: ProviderConfig = {
  protocol: "openai-compatible",
  base_url: "",
  api_key: "",
  model: "",
};

type Phase = "idle" | "uploading" | "recon" | "locate" | "done" | "error";

export default function Home() {
  const [chatTurns, setChatTurns] = useState<ChatTurn[]>([]);
  const [chatBusy, setChatBusy] = useState(false);
  const [chatScope, setChatScope] = useState<"current" | "all">("current");
  const chatRef = useRef<HTMLDivElement>(null);

  const [notice, setNotice] = useState<string | null>(null);

  const [plan, setPlan] = useState<Plan | null>(null);

  const [provider, setProvider] = useState<ProviderConfig>(EMPTY_PROVIDER);
  const [rememberKey, setRememberKey] = useState(false);
  const [smoke, setSmoke] = useState<SmokeResult | null>(null);
  const [smokeBusy, setSmokeBusy] = useState(false);
  const [backendUp, setBackendUp] = useState<boolean | null>(null);

  const [file, setFile] = useState<File | null>(null);
  const [repoUrl, setRepoUrl] = useState("");
  const [locateBusy, setLocateBusy] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);
  const [paper, setPaper] = useState<PaperMeta | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  // 行动轨迹：宽屏是右侧常驻长条侧栏（窄、不占正文），默认展开——
  // 用户要求"不论在哪个步骤都能看到轨迹的实时情况"。用户的选择记在 localStorage 里
  const [timelineOpen, setTimelineOpen] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);

  /** 报错可能是第三方返回的一大页 HTML：先压成一句，完整内容丢控制台，别糊满屏幕。 */
  const reportError = useCallback((caught: unknown) => {
    const text = typeof caught === "string" ? caught : String(caught);
    console.error(caught);
    setError(text.length > 400 ? `${text.slice(0, 400)}…（完整信息见浏览器控制台）` : text);
  }, []);

  const sourceRef = useRef<EventSource | null>(null);
  // 事件数组的镜像：openStream 需要在"重开流"时知道已经收到哪一条了，但又不该因此重建回调
  // 清单出来后默认全选：省得用户以为"不勾就等于全跑"，也避免只勾一条时的意外
  const autoSelectedRef = useRef(false);
  const eventsRef = useRef<RunEvent[]>([]);
  useEffect(() => {
    eventsRef.current = events;
  }, [events]);

  // 阅读区始终跟随当前创新点；点击具体引用只覆盖当前条目的阅读目标。
  const readerRef = useRef<HTMLDivElement>(null);
  const [setupOpen, setSetupOpen] = useState(true);
  const [readerOpen, setReaderOpen] = useState(true);
  const [readerTab, setReaderTab] = useState<"paper" | "code">("paper");
  const [activeId, setActiveId] = useState("");
  const activeInnovation = plan?.innovations.find((item) => item.id === activeId) ?? plan?.innovations[0];
  const findings = useMemo<Finding[]>(
    () => Array.from(new Map(events
      .filter((event) => event.type === "finding")
      .map((event) => {
        const finding = event.data.finding as Finding;
        return [finding.id, finding] as const;
      })).values()).filter((finding) => plan?.innovations.some((item) => item.id === finding.id)),
    [events, plan],
  );
  const activeFinding = findings.find((finding) => finding.id === activeInnovation?.id);
  const chatContext = useMemo(() => chatScope === "current" && activeInnovation ? { id: activeInnovation.id, name: activeInnovation.name } : null, [chatScope, activeInnovation]);
  const repoVersion = useMemo(() => [...events].reverse().find((event) => event.type === "repo_ready")?.id ?? 0, [events]);
  const { paperPane, codePane, selectPaper, selectCode, reset: clearReader } = useReader(runId, activeInnovation, activeFinding, repoVersion);

  const activateInnovation = (id: string) => {
    if (id === activeInnovation?.id) return;
    setActiveId(id);
    clearReader();
  };

  const scrollToReader = useCallback(() => {
    setReaderOpen(true);
    requestAnimationFrame(() => readerRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  }, []);

  const openPaper = (page: number, quote: string) => {
    selectPaper({ page, quote });
    setReaderTab("paper");
    scrollToReader();
  };

  const openCode = (path: string, start: number, end: number, why: string) => {
    selectCode({ path, start, end, why });
    setReaderTab("code");
    scrollToReader();
  };

  const focusChat = useCallback(() => {
    chatRef.current?.querySelector("textarea")?.focus({ preventScroll: true });
    chatRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

  // ---- 后端健康检查 + 恢复上次填的 provider（**默认不恢复 api_key**）----
  useEffect(() => {
    fetch(healthUrl())
      .then((response) => setBackendUp(response.ok))
      .catch(() => setBackendUp(false));

    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const saved = JSON.parse(raw) as Partial<ProviderConfig> & { remember_key?: boolean };
        setProvider((current) => ({
          ...current,
          ...saved,
          api_key: saved.remember_key ? (saved.api_key ?? "") : "",
        }));
        setRememberKey(Boolean(saved.remember_key));
      }
    } catch {
      /* localStorage 里的脏数据不该让页面挂掉 */
    }

    return () => sourceRef.current?.close();
  }, []);

  const saveProvider = useCallback(() => {
    const payload = { ...provider, api_key: rememberKey ? provider.api_key : "", remember_key: rememberKey };
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  }, [provider, rememberKey]);

  const openStream = useCallback((id: string) => {
    sourceRef.current?.close();
    // 只订阅"还没收到过"的部分：这样阶段 B 重开流时不会重放阶段 A 的 run_end
    const lastSeen = eventsRef.current.reduce((max, event) => Math.max(max, event.id), 0);
    const source = new EventSource(eventsUrl(id, lastSeen));
    sourceRef.current = source;

    // 后端发的是具名事件，所以必须逐个类型注册监听（漏一个就静默收不到）
    for (const type of EVENT_TYPES) {
      source.addEventListener(type, (event) => {
        const parsed = JSON.parse((event as MessageEvent).data) as RunEvent;
        setEvents((previous) =>
          previous.some((item) => item.id === parsed.id) ? previous : [...previous, parsed],
        );
        if (parsed.type === "run_end") {
          setPhase("done");
          source.close();
        }
      });
    }
    // 断线不用管：EventSource 会自动重连并带上 Last-Event-ID，后端只补发缺的那几条
    source.onerror = () => undefined;
  }, []);

  const handleSmoke = useCallback(async () => {
    setSmokeBusy(true);
    setError(null);
    try {
      setSmoke(await smokeTest(provider));
    } catch (caught) {
      reportError(caught);
    } finally {
      setSmokeBusy(false);
    }
  }, [provider, reportError]);

  const handleStart = useCallback(async () => {
    if (!file) {
      setError("还没选论文 —— 在「论文 PDF」中选一个文件");
      return;
    }
    if (!provider.api_key || !provider.model) {
      setError("还没配好模型 —— 在「模型设置」中填 api_key 和模型名（建议先点「运行自检」）");
      return;
    }
    setError(null);
    setEvents([]);
    eventsRef.current = [];
    clearReader();
    setActiveId("");
    setReaderOpen(true);
    setReaderTab("paper");
    setSelected(new Set());
    autoSelectedRef.current = false;
    setPlan(null);
    setNotice(null);
    setChatTurns([]);
    setChatScope("current");
    setPaper(null);
    setRunId(null);
    try {
      setPhase("uploading");
      const created = await createRun(file, provider);
      setRunId(created.run_id);
      setPaper(created.paper);
      setSetupOpen(false);
      openStream(created.run_id);
      setPhase("recon");
      await startRecon(created.run_id, provider);
    } catch (caught) {
      setError(String(caught));
      setPhase("error");
    }
  }, [file, provider, openStream, clearReader]);

  const handleLocate = useCallback(async () => {
    if (!runId) {
      setError("还没有可定位的目标：先上传论文跑一次侦察，或者在原文文本里划选一段自己加一个目标");
      return;
    }
    if (selected.size === 0) {
      setError("还没勾选任何目标 —— 在「创新点目录」中勾上要定位的条目");
      return;
    }
    if (!repoUrl.trim()) {
      setError(
        "还没填代码仓库地址 —— 在「代码仓库」中填。" +
          "本地演示可以填 backend/tests/fixtures/sample_repo 的**绝对路径**" +
          "（需要后端带 PAPERLENS_ALLOW_LOCAL_REPO_PATHS=true 启动）",
      );
      return;
    }
    setError(null);
    setLocateBusy(true);
    try {
      // 重新开流：阶段 A 结束时前端已经把 EventSource 关掉了。
      // 后端会重放全部历史（按 id 去重），所以不会丢事件也不会重复。
      openStream(runId);
      setPhase("locate");
      await startLocate(runId, provider, repoUrl.trim(), Array.from(selected));
    } catch (caught) {
      setError(String(caught));
      setPhase("error");
    } finally {
      setLocateBusy(false);
    }
  }, [runId, repoUrl, provider, selected, openStream]);

  const planFromEvent = useMemo<Plan | null>(() => {
    const ready = events.find((event) => event.type === "plan_ready");
    return (ready?.data.plan as Plan) ?? null;
  }, [events]);

  // 清单以**服务器为准**：用户自己加的目标只存在于服务端的 plan.json 里，
  // 光靠事件回放会把它丢掉（刷新页面就会丢）。所以事件到了之后再去拉一次真清单。
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    fetchPlan(runId)
      .then((payload: { plan: Plan | null }) => {
        if (cancelled) return;
        if (payload?.plan) setPlan(payload.plan);
        else if (planFromEvent) setPlan(planFromEvent);
      })
      .catch(() => {
        if (!cancelled && planFromEvent) setPlan(planFromEvent);
      });
    return () => {
      cancelled = true;
    };
  }, [planFromEvent, runId]);

  // ---- 追问对话 ----
  useEffect(() => {
    try {
      setTimelineOpen(window.localStorage.getItem("paperlens.timelineOpen") !== "0");
    } catch {
      /* 隐私模式下拿不到 localStorage 也无所谓 */
    }
  }, []);

  const toggleTimeline = useCallback(() => {
    setTimelineOpen((open) => {
      const next = !open;
      try {
        window.localStorage.setItem("paperlens.timelineOpen", next ? "1" : "0");
      } catch {
        /* 同上 */
      }
      return next;
    });
  }, []);

  const refreshChat = useCallback(async () => {
    if (!runId) return;
    try {
      const payload = await fetchChat(runId);
      setChatTurns((payload.turns ?? []) as ChatTurn[]);
    } catch {
      /* 拉不到就先用事件里的内容，不打断用户 */
    }
  }, [runId]);

  useEffect(() => {
    if (runId) void refreshChat();
  }, [runId, refreshChat]);

  // 每落盘一轮回答就重新拉一次：以服务端的对话记录为准（刷新页面也不丢）
  const chatReplyCount = useMemo(
    () => events.filter((event) => event.type === "chat_reply").length,
    [events],
  );
  useEffect(() => {
    if (chatReplyCount > 0) {
      setChatBusy(false);
      void refreshChat();
    }
  }, [chatReplyCount, refreshChat]);

  /**
   * 正在流式输出的那一轮回答。
   *
   * 关键：**一旦这轮回答已经落盘（chat_reply 出现在 chat_user 之后），就返回空**。
   * 否则 ChatPanel 会同时渲染"服务端对话记录里的回答"和"事件流里残留的流式文本"——
   * 用户看到同一段回答出现两次（2026-09-16 用户实测反馈：
   * "一句疑问 agent 会回复我两次内容"）。后端本身有并发保护（同一 run 同时只允许一个阶段，409），
   * 所以这纯粹是前端重复渲染。
   */
  const chatStreaming = useMemo(() => {
    let lastUserIndex = -1;
    let lastReplyIndex = -1;
    events.forEach((event, index) => {
      if (event.type === "chat_user") lastUserIndex = index;
      if (event.type === "chat_reply") lastReplyIndex = index;
    });
    if (lastUserIndex < 0 || lastReplyIndex > lastUserIndex) return "";
    return events
      .slice(lastUserIndex + 1)
      .filter((event) => event.type === "assistant_text")
      .map((event) => event.data.delta as string)
      .join("");
  }, [events]);

  const sendChat = useCallback(
    async (message: string) => {
      if (!runId) {
        setError("请先上传论文并跑一次分析");
        return;
      }
      setError(null);
      setChatBusy(true);
      setChatTurns((current) => [...current, { role: "user", text: message, ts: Date.now() }]);
      try {
        openStream(runId); // 阶段结束会关流，追问前重新连上
        await postChat(runId, {
          provider,
          message,
          context_ids: chatContext ? [chatContext.id] : [],
        });
      } catch (caught) {
        setError(String(caught));
        setChatBusy(false);
        void refreshChat();
      }
    },
    [runId, provider, chatContext, openStream, refreshChat],
  );

  const askAbout = (id: string) => {
    activateInnovation(id);
    setChatScope("current");
    focusChat();
  };

  /** 在原文里划选一段 → 变成一个定位目标 */
  const addTarget = useCallback(
    async (quote: string, page: number) => {
      if (!runId) {
        setError("请先上传论文并跑一次侦察（或直接上传后即可添加目标）");
        return;
      }
      try {
        setError(null);
        const payload = await addPlanItem(runId, { page, quote });
        setPlan(payload.plan);
        setSelected((current) => new Set(current).add(payload.added.id));
        setNotice(
          `已加入目标「${payload.added.name}」（${payload.added.id}）。可以改名字或线索，然后点「开始定位」。`,
        );
      } catch (caught) {
        setError(String(caught));
      }
    },
    [runId],
  );

  const renameTarget = useCallback(
    async (itemId: string, name: string) => {
      if (!runId) return;
      try {
        const payload = await patchPlanItem(runId, itemId, { name });
        setPlan(payload.plan);
      } catch (caught) {
        setError(String(caught));
      }
    },
    [runId],
  );

  const removeTarget = useCallback(
    async (itemId: string) => {
      if (!runId) return;
      try {
        const payload = await deletePlanItem(runId, itemId);
        setPlan(payload.plan);
        if (activeInnovation?.id === itemId) {
          setActiveId("");
          clearReader();
        }
        setSelected((current) => {
          const next = new Set(current);
          next.delete(itemId);
          return next;
        });
      } catch (caught) {
        setError(String(caught));
      }
    },
    [runId, activeInnovation, clearReader],
  );

  useEffect(() => {
    const generated = plan?.innovations.filter((item) => item.source !== "user") ?? [];
    if (generated.length > 0 && !autoSelectedRef.current) {
      autoSelectedRef.current = true;
      setSelected((current) => {
        const next = new Set(current);
        for (const item of generated) next.add(item.id);
        return next;
      });
    }
  }, [plan]);

  const verification = useMemo<VerificationSummary | null>(() => {
    const done = [...events].reverse().find((event) => event.type === "verification_done");
    return (done?.data.summary as VerificationSummary) ?? null;
  }, [events]);
  const missingIds = useMemo<string[]>(() => {
    const done = [...events].reverse().find((event) => event.type === "verification_done");
    return (done?.data.missing_ids as string[]) ?? [];
  }, [events]);
  const filesTotal = useMemo<number | null>(() => {
    const ready = [...events].reverse().find((event) => event.type === "repo_ready");
    return (ready?.data.repo?.files_total as number) ?? null;
  }, [events]);
  const commitSha = useMemo<string | null>(() => {
    const ready = [...events].reverse().find((event) => event.type === "repo_ready");
    return (ready?.data.repo?.commit_sha as string) ?? null;
  }, [events]);

  const runEnd = useMemo(() => [...events].reverse().find((event) => event.type === "run_end")?.data, [events]);
  const paperReady = useMemo(
    () => events.find((event) => event.type === "paper_ready")?.data.paper as PaperMeta | undefined,
    [events],
  );

  const toggle = (id: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const toggleAll = () =>
    setSelected((current) =>
      plan && plan.innovations.every((item) => current.has(item.id))
        ? new Set()
        : new Set(plan?.innovations.map((item) => item.id) ?? []),
    );

  const busy = phase === "uploading" || phase === "recon" || phase === "locate";

  // "点了没反应"的根治办法：把还缺什么直接写在按钮旁边
  const reconBlockers = [
    !file && "先选一篇 PDF",
    !provider.api_key && "在「模型设置」中填 api_key",
    !provider.model && "在「模型设置」中填模型名",
  ].filter(Boolean) as string[];

  const locateBlockers = [
    !runId && "先上传论文并跑一次侦察（或先自己加一个目标）",
    selected.size === 0 && "先勾选至少一条要定位的目标",
    !repoUrl.trim() && "在「代码仓库」中填代码仓库地址",
    !provider.api_key && "在「模型设置」中填 api_key",
  ].filter(Boolean) as string[];

  const phaseLabel = { idle: "准备论文与仓库", uploading: "正在上传论文", recon: "正在侦察论文", locate: "正在定位代码", done: "本轮已结束", error: "运行出错" }[phase];

  return (
    <main className="mx-auto w-full max-w-[1560px] flex-1 px-4 py-4 sm:px-6">
      <header className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">PaperLens <span className="ml-2 text-xs font-normal text-neutral-500">论文与代码，一起读懂</span></h1>
          <p className="mt-1 text-xs text-neutral-500">从核心创新点出发，阅读解释，回到原文与实现。</p>
        </div>
        <span className="text-xs text-neutral-500" title={`后端 ${API_BASE}`}>{backendUp === null ? "连接检查中…" : backendUp ? "● 服务已连接" : "服务未连接，请启动后端"}</span>
      </header>

      {error && <div role="alert" className="sticky top-2 z-50 mb-4 flex items-start gap-2 rounded-lg border border-red-300 bg-red-50 p-3 text-sm shadow-lg dark:border-red-900 dark:bg-red-950 dark:text-red-100">
        <span className="min-w-0 flex-1 break-words">{error}</span>
        <button type="button" onClick={() => setError(null)} className="shrink-0 text-xs underline">关闭</button>
      </div>}
      {notice && <div role="status" className="sticky top-2 z-40 mb-4 flex items-start gap-2 rounded-lg border border-blue-300 bg-blue-50 p-3 text-sm dark:border-blue-900 dark:bg-blue-950 dark:text-blue-100">
        <span className="min-w-0 flex-1">{notice}</span>
        <button type="button" onClick={() => setNotice(null)} className="shrink-0 text-xs underline">知道了</button>
      </div>}

      <div className="run-status sticky top-0 z-30 mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-neutral-200 bg-white/95 px-4 py-3 backdrop-blur dark:border-neutral-800 dark:bg-neutral-950/95">
        <span className={`h-2 w-2 rounded-full ${busy ? "animate-pulse bg-teal-500" : phase === "error" ? "bg-red-500" : "bg-neutral-400"}`} />
        <span role="status" className="text-sm font-medium">{phaseLabel}</span>
        {plan && <span className="text-xs text-neutral-500">{plan.innovations.length} 条创新点 · {findings.length} 条结论</span>}
        {runId && <nav aria-label="阅读快捷入口" className="flex gap-3 text-xs text-teal-700 dark:text-teal-300"><button type="button" onClick={scrollToReader}>原文与代码</button><button type="button" onClick={focusChat}>追问</button></nav>}
        <button type="button" onClick={toggleTimeline} aria-expanded={timelineOpen} aria-controls="run-timeline run-timeline-sidebar" className="ml-auto text-xs text-neutral-500">{timelineOpen ? "收起轨迹" : "展开行动轨迹"} · {events.length}</button>
      </div>

      <div className="flex items-start gap-5">
        <div className="min-w-0 flex-1 space-y-4">
          <section className="setup-panel rounded-xl border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-950">
            <button type="button" onClick={() => setSetupOpen(!setupOpen)} aria-expanded={setupOpen} aria-controls="analysis-setup" className="flex w-full flex-wrap items-center gap-2 px-4 py-3 text-left">
              <span className="text-sm font-semibold">分析准备</span>
              <span className="min-w-0 flex-1 truncate text-xs text-neutral-500">{paper ? `${paper.filename} · ${paper.page_count} 页` : "选择模型、上传论文"}{provider.model ? ` · ${provider.model}` : ""}</span>
              <span className="text-xs text-teal-700 dark:text-teal-300">{setupOpen ? "收起 ↑" : "修改设置 / 更换论文 ↓"}</span>
            </button>
            <div id="analysis-setup" hidden={!setupOpen} className="border-t border-neutral-200 p-4 dark:border-neutral-800">
              <div className="grid items-start gap-5 md:grid-cols-2">
                <div className="min-w-0">
                  <ProviderForm value={provider} onChange={setProvider} smoke={smoke} smokeBusy={smokeBusy} onSmoke={handleSmoke} onSave={saveProvider} />
                  <label className="mt-3 flex items-start gap-2 text-xs text-neutral-500"><input type="checkbox" checked={rememberKey} onChange={(event) => setRememberKey(event.target.checked)} />把 api_key 存在这个浏览器里（默认不存；存了就等于交给 localStorage）</label>
                </div>
                <section className="min-w-0">
                  <h2 className="mb-3 text-sm font-semibold">论文 PDF</h2>
                  <input type="file" aria-label="论文 PDF" accept="application/pdf" onChange={(event) => setFile(event.target.files?.[0] ?? null)} className="block w-full text-xs file:mr-3 file:rounded file:border-0 file:bg-neutral-100 file:px-3 file:py-2 dark:file:bg-neutral-800" />
                  {paper && <p className="mt-2 break-words text-xs text-neutral-500">{paper.filename} · {paper.page_count} 页</p>}
                  <button type="button" onClick={handleStart} disabled={busy} className="primary-button mt-4 w-full">{phase === "uploading" ? "上传中…" : phase === "recon" ? "正在侦察论文…" : "上传并开始侦察"}</button>
                  {reconBlockers.length > 0 && <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">还差：{reconBlockers.join("、")}</p>}
                  <p className="mt-3 text-xs leading-6 text-neutral-500">先梳理论文的核心创新点，再选择要定位的实现。每条论文引用都会核对页码与原文。</p>
                  {paperReady && !runEnd && <p className="mt-2 text-xs text-neutral-500">论文已解析完成</p>}
                </section>
              </div>
            </div>
          </section>

          <section className="rounded-xl border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-950">
            <label htmlFor="repository" className="mb-2 block text-sm font-semibold">代码仓库</label>
            <div className="flex flex-col gap-2 sm:flex-row">
              <input id="repository" value={repoUrl} onChange={(event) => setRepoUrl(event.target.value)} placeholder="https://github.com/owner/repo" className="min-w-0 flex-1 rounded-lg border border-neutral-300 bg-transparent px-3 py-2 font-mono text-xs dark:border-neutral-700" />
              <button type="button" onClick={handleLocate} disabled={busy || locateBusy} className="primary-button shrink-0">{phase === "locate" ? "正在定位…" : `开始定位选中的 ${selected.size} 条`}</button>
            </div>
            {locateBlockers.length > 0 && <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">还差：{locateBlockers.join("、")}</p>}
          </section>

          <div id="run-timeline" className="xl:hidden" hidden={!timelineOpen}><Timeline events={events} variant="inline" /></div>

          <section className="innovation-workspace rounded-xl border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-950" aria-label="创新点阅读区">
            <header className="flex flex-wrap items-center gap-3 border-b border-neutral-200 px-5 py-4 dark:border-neutral-800">
              <h2 className="text-sm font-semibold">核心创新点 {plan && <span className="ml-1 font-normal text-neutral-500">{plan.innovations.length} 条</span>}</h2>
              <div className="ml-auto flex gap-4 text-xs text-teal-700 dark:text-teal-300">
                {runId && <button type="button" onClick={() => openPaper(1, "")}>浏览论文 / 划选目标 ↗</button>}
                {runId && <button type="button" onClick={focusChat}>追问 ↗</button>}
              </div>
            </header>
            {plan ? <>
              <details className="paper-summary border-b border-neutral-200 px-5 py-3 text-sm dark:border-neutral-800">
                <summary className="text-xs text-neutral-500">论文摘要与覆盖说明</summary>
                <p className="mt-3 whitespace-pre-wrap leading-7">{plan.paper_summary}</p>
                {plan.coverage_note && <p className="mt-2 text-xs text-neutral-500">覆盖说明：{plan.coverage_note}</p>}
              </details>
              <div className="innovation-layout">
                <PlanList key={runId} plan={plan} selected={selected} activeId={activeInnovation?.id ?? ""} findings={findings} missingIds={missingIds} busy={busy} onActivate={activateInnovation} onToggle={toggle} onSelectAll={toggleAll} />
                {activeInnovation && <div id="innovation-detail" role="tabpanel" aria-labelledby={`innovation-tab-${activeInnovation.id}`} tabIndex={0} className="innovation-detail">
                  <PlanDetail key={activeInnovation.id} active={activeInnovation} hasFinding={Boolean(activeFinding)} onRename={renameTarget} onDelete={removeTarget} onOpenPaper={openPaper} />
                  <ComparePanel key={`finding-${activeInnovation.id}`} finding={activeFinding} missing={missingIds.includes(activeInnovation.id)} busy={phase === "locate"} selected={selected.has(activeInnovation.id)} onOpenPaper={openPaper} onOpenCode={openCode} onAsk={askAbout} />
                </div>}
              </div>
            </> : <p className="px-5 py-8 text-sm text-neutral-500">{busy ? "正在阅读论文，创新点清单将在这里出现。" : "上传论文并开始侦察，在这里逐条阅读创新点及对应实现。"}</p>}
          </section>

          <section ref={readerRef} className="scroll-mt-24" aria-label="对照阅读器">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-sm font-semibold">对照阅读器 <span className="ml-2 text-xs font-normal text-neutral-500">{activeInnovation ? `当前：${activeInnovation.name}` : "原文与代码"}</span></h2>
              {runId && <button type="button" onClick={() => setReaderOpen(!readerOpen)} aria-expanded={readerOpen} aria-controls="reader-content" className="min-h-9 shrink-0 rounded px-2 text-xs text-teal-700 hover:bg-teal-50 dark:text-teal-300 dark:hover:bg-teal-950">{readerOpen ? "收起阅读器 ↑" : "展开阅读器 ↓"}</button>}
            </div>
            <div id="reader-content" hidden={!readerOpen}>
              {runId ? <Reader left={paperPane} right={codePane} walkthrough={activeFinding?.explanation.code_walkthrough} onOpenCode={openCode} activeTab={readerTab} onTabChange={setReaderTab} codeEmpty={phase === "locate" ? "正在定位代码，找到实现后会自动显示。" : activeFinding ? "这条创新点暂无可展示的代码引用。" : "开始定位后，这里会自动显示当前创新点的代码。"} pdfUrl={pdfUrl(runId)} pageImageUrl={(page: number) => paperPageImageUrl(runId, page)} onGoToPage={(page) => selectPaper({ page, quote: "" })} onSelectTarget={addTarget} /> : <p className="text-xs text-neutral-500">上传论文后，在这里对照原文与实现。</p>}
            </div>
          </section>

          <div ref={chatRef} hidden={!runId} className="scroll-mt-24">
            <ChatPanel key={runId} turns={chatTurns} streaming={chatStreaming} busy={chatBusy} context={chatContext} onClearContext={() => setChatScope(chatScope === "current" ? "all" : "current")} onSend={sendChat} disabled={!runId || busy} onOpenCitation={(path, start, end) => openCode(path, start, end, "来自追问对话")} />
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
