import type { RunEvent } from "./types";
import type { ChatTurn } from "../components/ChatPanel";

export function chatTurnFromEvent(event: RunEvent): ChatTurn {
  const data = event.data;
  return {
    role: event.type === "chat_user" ? "user" : "assistant",
    text: String(data.message ?? ""),
    ts: event.ts,
    tools_used: data.tools_used as string[] | undefined,
    citations: data.citations as ChatTurn["citations"],
    stopped_reason: data.stopped_reason as string | undefined,
    status: data.status as string | undefined,
    partial: data.partial as boolean | undefined,
  };
}

export function chatTurnsFromEvents(events: RunEvent[]): ChatTurn[] {
  return events.filter((event) => event.type === "chat_user" || event.type === "chat_reply").map(chatTurnFromEvent);
}

/** 只归约最后一次提问后的事件，旧阶段的结束、重放或文字不能混进本轮。 */
export function chatState(events: RunEvent[]) {
  const start = events.findLastIndex((event) => event.type === "chat_user");
  let status = "正在查阅资料";
  let streaming = "";
  let mode = "research";
  let ended = false;
  let reply: ChatTurn | null = null;
  let reason = "";
  if (start < 0) return { status, streaming, ended, reply, reason, userId: 0 };
  for (const event of events.slice(start + 1)) {
    const data = event.data;
    if (event.type === "step_start") {
      mode = String(data.mode ?? "research");
      streaming = "";
      status = mode === "answer" ? "正在整理完整回答" : "正在查阅资料";
    } else if (event.type === "tool_call") {
      streaming = "";
      status = `正在查阅 · ${String(data.tool)}`;
    } else if (event.type === "llm_retry") {
      status = `正在等待端点 · 约 ${Number(data.delay_seconds ?? 0)} 秒后继续`;
    } else if (event.type === "assistant_text") {
      if (mode === "answer") {
        streaming += String(data.delta ?? "");
        status = "正在回答";
      }
    } else if (event.type === "chat_reply") {
      reply = chatTurnFromEvent(event);
      streaming = "";
      ended = true;
      status = reply.partial || (reply.status && reply.status !== "ok") ? "本轮未完整交付" : "回答已完成";
      reason = reply.stopped_reason ?? "";
    } else if (event.type === "run_end") {
      ended = true;
      reason = String(data.stopped_reason ?? "");
      if (!reply) status = "本轮已结束，未收到完整回答";
    }
  }
  return { status, streaming, ended, reply, reason, userId: events[start].id };
}
