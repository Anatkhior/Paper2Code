"use client";

import { useEffect, useRef } from "react";

import type { RunEvent } from "@/lib/types";
import { toolArgsSummary, toolLabel } from "@/lib/toolLabels";
import Icon, { type IconName } from "@/components/Icon";
import Markdown from "@/components/Markdown";

type Tone = "muted" | "accent" | "ok" | "warn" | "fail" | "info";
type Item =
  | { kind: "event"; event: RunEvent; result?: RunEvent }
  | { kind: "turn"; event: RunEvent }
  | { kind: "text"; text: string; firstId: number };

const RUN_END: Record<string, { label: string; tone: Tone }> = {
  ok: { label: "完成", tone: "ok" },
  failed: { label: "失败", tone: "fail" },
  cancelled: { label: "已取消", tone: "warn" },
  interrupted: { label: "已中断", tone: "warn" },
};

/**
 * 连续的 assistant_text 增量合并成一段（打字机效果）；
 * tool_result 并回发起它的 tool_call，一次调用只占一行。
 */
function toItems(events: RunEvent[]): Item[] {
  const items: Item[] = [];
  const calls = new Map<string, Extract<Item, { kind: "event" }>>();
  for (const event of events) {
    if (event.type === "assistant_text") {
      const last = items[items.length - 1];
      const delta = String(event.data.delta ?? "");
      if (last && last.kind === "text") last.text += delta;
      else items.push({ kind: "text", text: delta, firstId: event.id });
      continue;
    }
    if (event.type === "step_start") { items.push({ kind: "turn", event }); continue; }
    const callId = String(event.data.call_id ?? "");
    const call = event.type === "tool_result" && callId ? calls.get(callId) : undefined;
    if (call) { call.result = event; continue; }
    const item: Extract<Item, { kind: "event" }> = { kind: "event", event };
    if (event.type === "tool_call" && callId) calls.set(callId, item);
    items.push(item);
  }
  return items;
}

function describe(event: RunEvent, result?: RunEvent): { icon: IconName; title: string; detail?: string; tone: Tone; quiet?: boolean } {
  const d = event.data;
  switch (event.type) {
    case "paper_ready":
      return { icon: "paper", tone: "accent", title: `论文就绪：${d.paper.page_count} 页`, detail: d.paper.title_guess || d.paper.filename };
    case "run_start":
      return { icon: "play", tone: "info", title: `开始阶段「${d.meta?.phase ?? "?"}」`, detail: `${d.provider} · 上限：${d.limits?.max_tool_calls} 次工具调用 / ${d.limits?.wall_clock_seconds}s` };
    case "tool_call": {
      const failed = Boolean(result?.data.is_error);
      const args = toolArgsSummary(d.args);
      return {
        icon: failed ? "alert" : "call",
        tone: failed ? "fail" : result ? "muted" : "accent",
        quiet: !failed,
        title: `${toolLabel(d.tool)}${args ? ` · ${args}` : ""}${failed ? "（出错）" : result ? "" : "…"}`,
        detail: [`${d.tool}(${JSON.stringify(d.args)})`, result && `${result.data.ms}ms · ${String(result.data.summary).slice(0, 220)}`].filter(Boolean).join("\n"),
      };
    }
    case "tool_result":
      return {
        icon: d.is_error ? "alert" : "result",
        tone: d.is_error ? "fail" : "muted",
        quiet: !d.is_error,
        title: `${toolLabel(d.tool)} 返回${d.is_error ? "（错误）" : ""}`,
        detail: `${d.ms}ms · ${String(d.summary).slice(0, 220)}`,
      };
    case "plan_ready":
      return { icon: "list", tone: "ok", title: `清单已产出：${d.plan?.innovations?.length ?? 0} 条创新点`, detail: d.warning || undefined };
    case "plan_updated":
      return { icon: "list", tone: "muted", quiet: true, title: `清单已更新：${d.plan?.innovations?.length ?? 0} 条创新点` };
    case "repo_cloning":
      return { icon: "download", tone: "info", title: "正在克隆仓库", detail: d.url };
    case "clone_progress":
      return { icon: "download", tone: "muted", quiet: true, title: d.text ?? "克隆中…" };
    case "repo_ready": {
      // 克隆与地址判定的提示：例如"只检出源码视图、跳过 N 个媒体文件"、
      // "本机解析到代理占位地址，local 模式已放行"。不显示出来，用户就不知道
      // 系统替他做了什么决定——判断依据必须可见。
      const notes: string[] = Array.isArray(d.repo.notes) ? d.repo.notes : [];
      const skipped: number = d.repo.skipped_files ?? 0;
      return {
        icon: "box",
        tone: notes.length ? "warn" : "info",
        title:
          `仓库就绪：${d.repo.files_total} 个文件 · ${d.repo.mb}MB` +
          (skipped ? ` · 另有 ${skipped} 个未下载（媒体/权重等，工具不读）` : "") +
          (d.repo.from_cache ? " · 本地缓存复用（未走网络）" : ""),
        detail: [`commit ${String(d.repo.commit_sha).slice(0, 12)}…`, ...notes.map((note) => `注意：${note}`)].join("\n"),
      };
    }
    case "verification_done": {
      const rate = Number(d.summary?.citation_verifiable_rate ?? 0);
      return {
        icon: "shield",
        tone: d.summary?.citations_failed ? "warn" : "ok",
        title: `引用核验：${d.summary?.citations_verified}/${d.summary?.citations_total} 通过（核验率 ${Math.round(rate * 100)}%）`,
        detail: d.missing_ids?.length ? `还有未提交的创新点：${d.missing_ids.join(", ")}` : undefined,
      };
    }
    case "finding":
      return { icon: "target", tone: "ok", title: `定位到：${d.finding?.name ?? d.innovation_id}` };
    case "chat_user":
      return { icon: "chat", tone: "info", title: "收到追问", detail: String(d.message ?? "").slice(0, 400) };
    case "chat_reply":
      return { icon: "chat", tone: d.status && d.status !== "ok" ? "warn" : "ok", title: d.status && d.status !== "ok" ? "回答未完整交付" : "回答已交付", detail: d.unverified ? `${d.unverified} 处代码位置未通过核对` : undefined };
    case "budget_warning":
      return { icon: "alert", tone: "warn", title: "预算告警", detail: d.reason };
    case "llm_retry":
      // 网关限流：必须让用户看到"在等，不是卡死"（一次定位要几十次调用，很容易撞限额）
      return {
        icon: "clock",
        tone: "warn",
        title: `端点限流，等待 ${d.delay_seconds}s 后重试（第 ${d.attempt} 次）`,
        detail: String(d.detail ?? "").slice(0, 200),
      };
    case "error":
      return { icon: "close", tone: "fail", title: `错误：${d.kind}`, detail: d.message };
    case "run_end": {
      const end = RUN_END[d.status] ?? { label: String(d.status), tone: "fail" as Tone };
      return {
        icon: "flag",
        tone: end.tone,
        title: `结束：${end.label}${d.stopped_reason ? `（${d.stopped_reason}）` : ""}`,
        detail: d.usage ? `轮数 ${d.turns ?? "?"} · 工具调用 ${d.usage.tool_calls} · 估算输入 ${d.usage.input_tokens} token · ${d.usage.seconds}s` : undefined,
      };
    }
    default:
      return { icon: "dot", tone: "muted", quiet: true, title: event.type, detail: JSON.stringify(d).slice(0, 200) };
  }
}

/** 相对第一条事件的用时：一分钟内精确到 0.1 秒，之后用分秒。 */
function elapsed(seconds: number) {
  if (seconds < 60) return `${Math.max(0, seconds).toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m${String(Math.floor(seconds % 60)).padStart(2, "0")}s`;
}

export default function Timeline({
  events,
  variant = "inline",
}: {
  events: RunEvent[];
  /** inline：嵌在正文里（窄屏用）；sidebar：整页右侧的长条侧栏（宽屏用，始终可见） */
  variant?: "inline" | "sidebar";
}) {
  const items = toItems(events);
  const scrollerRef = useRef<HTMLDivElement>(null);

  /**
   * 自动跟到最新一条，但**只滚这个容器，绝不滚动窗口**。
   *
   * 原来用 `bottomRef.scrollIntoView({block:"end"})`：scrollIntoView 会一路滚所有可滚祖先，
   * 包括 window —— 于是"追问"产生新事件时，页面会被拽到时间线（页面靠上的位置），
   * 用户正在看的追问面板被顶走（2026-09-16 用户实测反馈）。
   * 另外只有"用户本来就在底部"时才跟随，否则会打断他往回翻看历史。
   */
  useEffect(() => {
    const box = scrollerRef.current;
    if (!box) return;
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    if (!nearBottom) return;
    box.scrollTop = box.scrollHeight;
  }, [events.length]);

  if (events.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-line-strong p-5 text-sm leading-6 text-muted">
        上传论文后，这里实时显示阅读、搜索与核验进展。
      </div>
    );
  }

  const start = events[0]?.ts ?? 0;

  return (
    <div
      ref={scrollerRef}
      className={`timeline-${variant} scroll-area panel overflow-y-auto px-3 py-1 ${variant === "sidebar" ? "max-h-full" : "max-h-64"}`}
    >
      <ol>
        {items.map((item) => {
          if (item.kind === "text") {
            return (
              <li key={`text-${item.firstId}`} className="timeline-item">
                <span aria-hidden />
                <Markdown text={item.text} className="text-xs leading-5 text-muted" />
              </li>
            );
          }
          if (item.kind === "turn") {
            return <li key={item.event.id} className="timeline-turn">第 {item.event.data.turn} 轮</li>;
          }
          const info = describe(item.event, item.result);
          return (
            <li key={item.event.id} className="timeline-item" data-tone={info.tone} data-quiet={info.quiet || undefined}>
              <span className="timeline-icon"><Icon name={info.icon} size={12} /></span>
              <div className="min-w-0">
                <p className={`break-words text-xs leading-5 ${info.quiet ? "text-foreground/80" : "font-medium"}`} style={info.quiet ? undefined : { color: "var(--tone)" }}>
                  {item.event.type === "finding" ? <Markdown inline text={info.title} /> : info.title}
                </p>
                <div className="mt-0.5 flex items-start gap-2 text-[11px] leading-5 text-muted">
                  <span className="timeline-time shrink-0">{elapsed(item.event.ts - start)}</span>
                  {info.detail && <details className="min-w-0 flex-1">
                    <summary className="w-fit">详情</summary>
                    <p className="mt-1 whitespace-pre-wrap break-all font-mono text-[11px] leading-5">{info.detail}</p>
                  </details>}
                </div>
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
