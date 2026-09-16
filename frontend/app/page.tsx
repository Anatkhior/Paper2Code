"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import ChatPanel, { type ChatTurn } from "@/components/ChatPanel";
import ComparePanel from "@/components/ComparePanel";
import CoverageCard from "@/components/CoverageCard";
import PlanList from "@/components/PlanList";
import ProviderForm from "@/components/ProviderForm";
import Reader, { type CodePane, type PaperPane } from "@/components/Reader";
import Timeline from "@/components/Timeline";
import {
  API_BASE,
  addPlanItem,
  createRun,
  fetchChat,
  postChat,
  deletePlanItem,
  eventsUrl,
  fetchFile,
  fetchPaperPage,
  fetchPlan,
  healthUrl,
  pdfUrl,
  patchPlanItem,
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

  // ---- 双栏阅读器：左论文原文、右代码实现 ----
  const [paperPane, setPaperPane] = useState<PaperPane | null>(null);
  const [codePane, setCodePane] = useState<CodePane | null>(null);
  const readerRef = useRef<HTMLDivElement>(null);

  const scrollToReader = useCallback(() => {
    readerRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

  const openPaper = useCallback(
    async (page: number, quote: string) => {
      if (!runId) return;
      setPaperPane({ page, quote, loading: true, error: null });
      scrollToReader();
      try {
        const view = await fetchPaperPage(runId, page);
        setPaperPane({ page, quote, text: view.text, pageCount: view.page_count, loading: false, error: null });
      } catch (caught) {
        setPaperPane({ page, quote, loading: false, error: String(caught) });
      }
    },
    [runId, scrollToReader],
  );

  const openCode = useCallback(
    async (path: string, start: number, end: number, why: string) => {
      if (!runId) return;
      setCodePane({ path, start, end, why, lines: [], loading: true, error: null });
      scrollToReader();
      try {
        const view = await fetchFile(runId, path, start, end);
        setCodePane({
          path: view.path,
          start: view.line_start,
          end: view.line_end,
          why,
          lines: view.lines,
          total: view.total_lines,
          commit: view.commit_sha,
          sourceUrl: view.source_url,
          loading: false,
          error: null,
        });
      } catch (caught) {
        setCodePane({ path, start, end, why, lines: [], loading: false, error: String(caught) });
      }
    },
    [runId, scrollToReader],
  );

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
  }, [provider]);

  const handleStart = useCallback(async () => {
    if (!file) {
      setError("还没选论文 —— 在第 2 节「论文 PDF」里选一个文件");
      return;
    }
    if (!provider.api_key || !provider.model) {
      setError("还没配好模型 —— 在第 1 节填 api_key 和模型名（建议先点「运行自检」）");
      return;
    }
    setError(null);
    setEvents([]);
    setSelected(new Set());
    autoSelectedRef.current = false;
    setPlan(null);
    setNotice(null);
    setChatTurns([]);
    setChatContext(null);
    setPaper(null);
    setRunId(null);
    try {
      setPhase("uploading");
      const created = await createRun(file, provider);
      setRunId(created.run_id);
      setPaper(created.paper);
      openStream(created.run_id);
      setPhase("recon");
      await startRecon(created.run_id, provider);
    } catch (caught) {
      setError(String(caught));
      setPhase("error");
    }
  }, [file, provider, openStream]);

  const handleLocate = useCallback(async () => {
    if (!runId) {
      setError("还没有可定位的目标：先上传论文跑一次侦察，或者在左边原文里划选一段自己加一个目标");
      return;
    }
    if (selected.size === 0) {
      setError("还没勾选任何目标 —— 在第 3 节「创新点清单」里勾上要定位的条目");
      return;
    }
    if (!repoUrl.trim()) {
      setError(
        "还没填代码仓库地址 —— 在第 4 节「代码仓库」里填。" +
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
  const [plan, setPlan] = useState<Plan | null>(null);
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

  const [notice, setNotice] = useState<string | null>(null);

  // ---- 追问对话 ----
  const [chatTurns, setChatTurns] = useState<ChatTurn[]>([]);
  const [chatBusy, setChatBusy] = useState(false);
  const [chatContext, setChatContext] = useState<{ id: string; name: string } | null>(null);
  const chatRef = useRef<HTMLDivElement>(null);

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

  // 正在流式输出的那一轮回答
  const chatStreaming = useMemo(() => {
    let lastUserIndex = -1;
    events.forEach((event, index) => {
      if (event.type === "chat_user") lastUserIndex = index;
    });
    if (lastUserIndex < 0) return "";
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

  const askAbout = useCallback((id: string, name: string) => {
    setChatContext({ id, name });
    chatRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

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
        setSelected((current) => {
          const next = new Set(current);
          next.delete(itemId);
          return next;
        });
      } catch (caught) {
        setError(String(caught));
      }
    },
    [runId],
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

  const findings = useMemo<Finding[]>(
    () => events.filter((event) => event.type === "finding").map((event) => event.data.finding as Finding),
    [events],
  );
  const verification = useMemo<VerificationSummary | null>(() => {
    const done = [...events].reverse().find((event) => event.type === "verification_done");
    return (done?.data.summary as VerificationSummary) ?? null;
  }, [events]);
  const missingIds = useMemo<string[]>(() => {
    const done = [...events].reverse().find((event) => event.type === "verification_done");
    return (done?.data.missing_ids as string[]) ?? [];
  }, [events]);
  const filesTotal = useMemo<number | null>(() => {
    const ready = events.find((event) => event.type === "repo_ready");
    return (ready?.data.repo?.files_total as number) ?? null;
  }, [events]);
  const commitSha = useMemo<string | null>(() => {
    const ready = events.find((event) => event.type === "repo_ready");
    return (ready?.data.repo?.commit_sha as string) ?? null;
  }, [events]);

  const runEnd = useMemo(() => events.find((event) => event.type === "run_end")?.data, [events]);
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
      plan && current.size === plan.innovations.length
        ? new Set()
        : new Set(plan?.innovations.map((item) => item.id) ?? []),
    );

  const busy = phase === "uploading" || phase === "recon" || phase === "locate";

  // "点了没反应"的根治办法：把还缺什么直接写在按钮旁边
  const reconBlockers = [
    !file && "先选一篇 PDF",
    !provider.api_key && "在第 1 节填 api_key",
    !provider.model && "在第 1 节填模型名",
  ].filter(Boolean) as string[];

  const locateBlockers = [
    !runId && "先上传论文并跑一次侦察（或先自己加一个目标）",
    selected.size === 0 && "先勾选至少一条要定位的目标",
    !repoUrl.trim() && "在第 4 节填代码仓库地址",
    !provider.api_key && "在第 1 节填 api_key",
  ].filter(Boolean) as string[];

  return (
    <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-6">
      <header className="mb-5">
        <h1 className="text-xl font-semibold">PaperLens</h1>
        <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">
          上传论文 + 粘贴仓库链接 → Agent 自主探索，定位论文创新点对应的代码实现，每条引用都可机械核验。
        </p>
        <p className="mt-1 text-xs text-neutral-500">
          后端 {API_BASE}：
          {backendUp === null ? "检查中…" : backendUp ? "✅ 已连接" : "❌ 连不上（先在 backend 目录跑 uvicorn）"}
          {" · "}M1 侦察 + M2 定位与核验 + M3 对照界面
        </p>
      </header>

      {/* sticky：反馈必须出现在用户正在看的地方。
          之前它渲染在页面顶部，用户在第 3/4 节点按钮时提示在屏幕外，
          表现成"点了没反应"——这类静默失败比报错还糟。 */}
      {error && (
        <div className="sticky top-2 z-50 mb-4 flex items-start gap-2 rounded border border-red-300 bg-red-50 p-3 text-sm shadow-lg dark:border-red-900 dark:bg-red-950 dark:text-red-100">
          <span className="flex-1">{error}</span>
          <button
            type="button"
            onClick={() => setError(null)}
            className="shrink-0 text-xs underline"
          >
            关闭
          </button>
        </div>
      )}

      {notice && (
        <div className="sticky top-2 z-40 mb-4 flex items-start gap-2 rounded border border-blue-300 bg-blue-50 p-3 text-sm shadow-lg dark:border-blue-900 dark:bg-blue-950 dark:text-blue-100">
          <span className="flex-1">{notice}</span>
          <button type="button" onClick={() => setNotice(null)} className="text-xs underline">
            知道了
          </button>
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-[420px_1fr]">
        <div className="space-y-4">
          <ProviderForm
            value={provider}
            onChange={setProvider}
            smoke={smoke}
            smokeBusy={smokeBusy}
            onSmoke={handleSmoke}
            onSave={saveProvider}
          />

          <label className="flex items-center gap-2 text-xs text-neutral-500">
            <input
              type="checkbox"
              checked={rememberKey}
              onChange={(event) => setRememberKey(event.target.checked)}
            />
            把 api_key 存在这个浏览器里（默认不存；存了就等于交给 localStorage）
          </label>

          <section className="rounded-lg border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-950">
            <h2 className="mb-3 text-sm font-semibold">2. 论文 PDF</h2>
            <input
              type="file"
              accept="application/pdf"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              className="block w-full text-xs file:mr-3 file:rounded file:border-0 file:bg-neutral-100 file:px-3 file:py-1.5 file:text-xs dark:file:bg-neutral-800"
            />
            {paper && (
              <p className="mt-2 text-xs text-neutral-500">
                {paper.filename} · {paper.page_count} 页 · sha256 {paper.sha256.slice(0, 12)}…
              </p>
            )}
            <button
              type="button"
              onClick={handleStart}
              disabled={busy}
              className="mt-3 w-full rounded bg-neutral-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-40 dark:bg-neutral-100 dark:text-neutral-900"
            >
              {phase === "uploading"
                ? "上传中…"
                : phase === "recon"
                  ? "侦察中…（Agent 正在读论文）"
                  : "上传并开始侦察（阶段 A）"}
            </button>
            {reconBlockers.length > 0 && (
              <p className="mt-2 text-[11px] text-amber-600">还差：{reconBlockers.join("、")}</p>
            )}
            <p className="mt-2 text-[11px] leading-relaxed text-neutral-500">
              阶段 A 只读论文、不碰代码。它会产出 3-5 条核心创新点，每条都带页码和原文引用——
              <strong>引用会被后端逐条核验是否真的在那一页</strong>，编的会被打回去重做。
            </p>
            {runEnd && (
              <p className="mt-2 font-mono text-[11px] text-neutral-500">
                run_id {runId} · {runEnd.status} · {runEnd.stopped_reason} · 工具调用{" "}
                {runEnd.usage?.tool_calls} 次 · {runEnd.usage?.seconds}s
              </p>
            )}
            {paperReady && !runEnd && <p className="mt-2 text-[11px] text-neutral-500">论文已解析完成</p>}
          </section>

          <section>
            <h2 className="mb-2 text-sm font-semibold">3. 创新点清单（勾选后进入阶段 B）</h2>
            <PlanList
              plan={plan}
              selected={selected}
              onToggle={toggle}
              onSelectAll={toggleAll}
              onRename={renameTarget}
              onDelete={removeTarget}
            />
          </section>

          <section className="rounded-lg border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-950">
            <h2 className="mb-3 text-sm font-semibold">4. 代码仓库（阶段 B）</h2>
            <input
              value={repoUrl}
              onChange={(event) => setRepoUrl(event.target.value)}
              placeholder="https://github.com/owner/repo"
              className="w-full rounded border border-neutral-300 bg-transparent px-2 py-1 font-mono text-xs dark:border-neutral-700"
            />
            <button
              type="button"
              onClick={handleLocate}
              disabled={locateBusy}
              className="mt-3 w-full rounded bg-neutral-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-40 dark:bg-neutral-100 dark:text-neutral-900"
            >
              {phase === "locate"
                ? "定位中…（Agent 正在仓库里探索）"
                : `开始定位选中的 ${selected.size} 条（阶段 B）`}
            </button>
            {locateBlockers.length > 0 && (
              <p className="mt-2 text-[11px] text-amber-600">还差：{locateBlockers.join("、")}</p>
            )}
            <p className="mt-2 text-[11px] leading-relaxed text-neutral-500">
              只允许 https 的 github.com / gitlab.com 地址；会做浅克隆并跳过依赖目录，
              <strong>且绝不执行仓库里的任何代码</strong>。引用会锚定到具体 commit，之后仓库怎么变都不影响你的解读。
            </p>
          </section>
        </div>

        <div className="space-y-4">
          <section>
            <h2 className="mb-2 text-sm font-semibold">5. Agent 行动轨迹（实时）</h2>
            <Timeline events={events} />
          </section>

        </div>
      </div>

      {/* 下面是这份系统的交付物本体：论文证据 ↔ 代码引用的双栏对照 */}
      <div className="mt-4 space-y-4">
        <CoverageCard
          events={events}
          verification={verification}
          missingIds={missingIds}
          filesTotal={filesTotal}
        />

        <section ref={readerRef} className="scroll-mt-4">
          <h2 className="mb-2 text-sm font-semibold">
            6. 对照阅读器
            <span className="ml-2 text-[11px] font-normal text-neutral-500">
              左右各自独立滚动，被引用的部分会高亮
            </span>
          </h2>
          <Reader
            left={paperPane}
            right={codePane}
            pdfUrl={runId ? pdfUrl(runId) : null}
            onGoToPage={(page) => openPaper(page, paperPane?.quote ?? "")}
            onSelectTarget={addTarget}
          />
        </section>

        <div ref={chatRef} className="scroll-mt-4">
          <ChatPanel
            turns={chatTurns}
            streaming={chatStreaming}
            busy={chatBusy}
            context={chatContext}
            onClearContext={() => setChatContext(null)}
            onSend={sendChat}
            disabled={!runId}
            onOpenCitation={(path, start, end) => openCode(path, start, end, "来自追问对话")}
          />
        </div>

        <section>
          <h2 className="mb-2 text-sm font-semibold">
            8. 逐条结论
            <span className="ml-2 text-[11px] font-normal text-neutral-500">
              点任意引用，上方的对照阅读器会跳到对应位置（追问也在这里就能用）
            </span>
          </h2>
          <ComparePanel
            findings={findings}
            verification={verification}
            missingIds={missingIds}
            commitSha={commitSha}
            busy={phase === "locate"}
            onOpenPaper={openPaper}
            onOpenCode={openCode}
            onAsk={askAbout}
          />
        </section>

      </div>
    </main>
  );
}
