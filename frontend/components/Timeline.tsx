"use client";

import { useEffect, useRef } from "react";

import type { RunEvent } from "@/lib/types";

type Item =
  | { kind: "event"; event: RunEvent }
  | { kind: "text"; text: string; firstId: number };

/** 把连续的 assistant_text 增量合并成一段（打字机效果），其余事件各占一行。 */
function toItems(events: RunEvent[]): Item[] {
  const items: Item[] = [];
  for (const event of events) {
    if (event.type === "assistant_text") {
      const last = items[items.length - 1];
      const delta: string = event.data.delta ?? "";
      if (last && last.kind === "text") last.text += delta;
      else items.push({ kind: "text", text: delta, firstId: event.id });
      continue;
    }
    items.push({ kind: "event", event });
  }
  return items;
}

function describe(event: RunEvent): { icon: string; title: string; detail?: string; tone: string } {
  const d = event.data;
  switch (event.type) {
    case "paper_ready":
      return { icon: "📄", tone: "text-neutral-700 dark:text-neutral-300", title: `论文就绪：${d.paper.page_count} 页`, detail: d.paper.title_guess || d.paper.filename };
    case "run_start":
      return { icon: "▶", tone: "text-blue-600", title: `开始阶段「${d.meta?.phase ?? "?"}」`, detail: `${d.provider} · 上限：${d.limits?.max_tool_calls} 次工具调用 / ${d.limits?.wall_clock_seconds}s` };
    case "step_start":
      return { icon: "·", tone: "text-neutral-400", title: `第 ${d.turn} 轮` };
    case "tool_call":
      return { icon: "🔧", tone: "text-amber-600", title: d.tool, detail: JSON.stringify(d.args) };
    case "tool_result":
      return {
        icon: d.is_error ? "⚠" : "↳",
        tone: d.is_error ? "text-red-600" : "text-neutral-500",
        title: `${d.tool} 返回${d.is_error ? "（错误）" : ""}`,
        detail: `${d.ms}ms · ${String(d.summary).slice(0, 220)}`,
      };
    case "plan_ready":
      return { icon: "✅", tone: "text-green-600", title: `清单已产出：${d.plan?.innovations?.length ?? 0} 条创新点`, detail: d.warning || undefined };
    case "repo_cloning":
      return { icon: "⤓", tone: "text-blue-600", title: "正在克隆仓库", detail: d.url };
    case "clone_progress":
      return { icon: "⤓", tone: "text-neutral-500", title: d.text ?? "克隆中…" };
    case "repo_ready": {
      // 克隆与地址判定的提示：例如"只检出源码视图、跳过 N 个媒体文件"、
      // "本机解析到代理占位地址，local 模式已放行"。不显示出来，用户就不知道
      // 系统替他做了什么决定——判断依据必须可见。
      const notes: string[] = Array.isArray(d.repo.notes) ? d.repo.notes : [];
      const skipped: number = d.repo.skipped_files ?? 0;
      return {
        icon: "📦",
        tone: "text-blue-600",
        title:
          `仓库就绪：${d.repo.files_total} 个文件 · ${d.repo.mb}MB` +
          (skipped ? ` · 另有 ${skipped} 个未下载（媒体/权重等，工具不读）` : "") +
          (d.repo.from_cache ? " · 本地缓存复用（未走网络）" : ""),
        detail: [`commit ${String(d.repo.commit_sha).slice(0, 12)}…`, ...notes.map((note) => `⚠ ${note}`)].join(
          " · ",
        ),
      };
    }
    case "verification_done":
      return {
        icon: "🧾",
        tone: d.summary?.citations_failed ? "text-amber-600" : "text-green-600",
        title: `引用核验：${d.summary?.citations_verified}/${d.summary?.citations_total} 通过（核验率 ${(d.summary?.citation_verifiable_rate ?? 0) * 100}%）`,
        detail: d.missing_ids?.length ? `还有未提交的创新点：${d.missing_ids.join(", ")}` : undefined,
      };
    case "finding":
      return { icon: "🎯", tone: "text-green-600", title: `定位到：${d.finding?.name ?? d.innovation_id}` };
    case "budget_warning":
      return { icon: "💰", tone: "text-amber-600", title: "预算告警", detail: d.reason };
    case "llm_retry":
      // 网关限流：必须让用户看到"在等，不是卡死"（一次定位要几十次调用，很容易撞限额）
      return {
        icon: "⏳",
        tone: "text-amber-600",
        title: `端点限流，等待 ${d.delay_seconds}s 后重试（第 ${d.attempt} 次）`,
        detail: String(d.detail ?? "").slice(0, 200),
      };
    case "error":
      return { icon: "✖", tone: "text-red-600", title: `错误：${d.kind}`, detail: d.message };
    case "run_end":
      return {
        icon: "🏁",
        tone: d.status === "ok" ? "text-neutral-700 dark:text-neutral-300" : "text-red-600",
        title: `结束：${d.status}（${d.stopped_reason}）`,
        detail: `轮数 ${d.turns} · 工具调用 ${d.usage?.tool_calls} · 估算输入 ${d.usage?.input_tokens} token · ${d.usage?.seconds}s`,
      };
    default:
      return { icon: "•", tone: "text-neutral-500", title: event.type, detail: JSON.stringify(d).slice(0, 200) };
  }
}

export default function Timeline({ events }: { events: RunEvent[] }) {
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
      <div className="rounded-lg border border-dashed border-neutral-300 p-6 text-sm text-neutral-500 dark:border-neutral-800">
        还没有事件。上传论文并开始侦察后，Agent 的每一步行动都会实时出现在这里。
        <p className="mt-2 text-xs">
          注意：这里展示的是**可观测的行动轨迹**（调了什么工具、参数是什么、返回了什么），
          不是模型的思维链——OpenAI/Anthropic 的 API 并不返回可展示的原始推理过程。
        </p>
      </div>
    );
  }

  const start = events[0]?.ts ?? 0;

  return (
    <div
      ref={scrollerRef}
      className="max-h-[70vh] overflow-y-auto rounded-lg border border-neutral-200 bg-white p-3 dark:border-neutral-800 dark:bg-neutral-950"
    >
      <ol className="space-y-1.5">
        {items.map((item) => {
          if (item.kind === "text") {
            return (
              <li key={`text-${item.firstId}`} className="ml-6 whitespace-pre-wrap text-sm text-neutral-800 dark:text-neutral-200">
                {item.text}
              </li>
            );
          }
          const info = describe(item.event);
          const offset = ((item.event.ts - start) * 1000).toFixed(0);
          return (
            <li key={item.event.id} className="flex gap-2 text-sm">
              <span className="w-14 shrink-0 text-right font-mono text-[11px] text-neutral-400">{offset}ms</span>
              <span className={info.tone}>{info.icon}</span>
              <span className="min-w-0 flex-1">
                <span className={`font-medium ${info.tone}`}>{info.title}</span>
                {info.detail && (
                  <span className="ml-2 break-all font-mono text-[11px] text-neutral-500">{info.detail}</span>
                )}
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
