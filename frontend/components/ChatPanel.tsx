"use client";

import { useEffect, useRef, useState } from "react";

import Icon from "@/components/Icon";
import Markdown from "@/components/Markdown";
import { toolLabel } from "@/lib/toolLabels";

export interface ChatCitation {
  path: string;
  line_start: number;
  line_end: number;
  verified: boolean;
  reason?: string;
}

export interface ChatTurn {
  provider?: import("@/lib/types").ProviderSummary;
  operation_id?: string;
  output_language?: string;
  role: "user" | "assistant";
  text: string;
  ts?: number;
  tools_used?: string[];
  citations?: ChatCitation[];
  stopped_reason?: string;
  status?: string;
  partial?: boolean;
}

interface Props {
  turns: ChatTurn[];
  /** 正在流式输出的那一轮回答（还没落盘） */
  streaming: string;
  busy: boolean;
  progress?: string;
  incomplete?: boolean;
  stoppedReason?: string;
  context: { id: string; name: string } | null;
  canFocusInnovation?: boolean;
  onClearContext: () => void;
  onSend: (message: string) => void;
  disabled?: boolean;
  /** 点引用 → 让上方阅读器跳过去 */
  onOpenCitation?: (path: string, start: number, end: number) => void;
}

/**
 * 追问对话面板。
 *
 * 两个刻意的设计：
 * 1. **每轮都显示"它这次查了什么"**：Agent 可以自己去翻论文和代码，
 *    用户有权知道它到底是查过才回答，还是凭印象回答。
 * 2. **回答里的代码位置带核验徽章**：回答是自由文本，但里面每个 `文件:行号`
 *    都被后端拿去 git 对象里核对过——核不过的直接标红，不让追问变成新的幻觉来源。
 */
export default function ChatPanel({
  turns,
  streaming,
  busy,
  progress,
  incomplete = false,
  stoppedReason,
  context,
  canFocusInnovation = true,
  onClearContext,
  onSend,
  disabled,
  onOpenCitation,
}: Props) {
  const [draft, setDraft] = useState("");
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);

  const copyReply = async (text: string, index: number) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedIndex(index);
      window.setTimeout(() => setCopiedIndex((current) => (current === index ? null : current)), 1600);
    } catch { /* ignore clipboard failures */ }
  };

  /**
   * 同 Timeline：只滚自己的容器，不碰窗口（scrollIntoView 会把整个页面也滚走），
   * 并且只在你本来就在底部时才跟随新消息。
   */
  useEffect(() => {
    const box = scrollerRef.current;
    if (!box) return;
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    if (!nearBottom) return;
    box.scrollTop = box.scrollHeight;
  }, [turns.length, streaming]);

  const submit = () => {
    const message = draft.trim();
    if (!message || busy || disabled) return;
    onSend(message);
    setDraft("");
  };

  return (
    <section className="panel">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-line px-4 py-3">
        <h2 className="text-sm font-semibold">追问</h2>
        <span className="text-xs text-muted">
          继续问原理、实现细节，或让它对照论文解释代码。
        </span>
      </header>

      {(busy || incomplete) && <p role="status" aria-live="polite" data-tone={busy ? "run" : "warn"} className="flex items-center gap-2 border-b border-line px-4 py-2 text-sm text-muted">
        <span className="status-dot" />{progress || "正在查阅资料"}{!busy && stoppedReason ? `：${stoppedReason}` : ""}
      </p>}
      <div ref={scrollerRef} hidden={turns.length === 0 && !streaming} className="scroll-area max-h-[56vh] space-y-4 overflow-auto px-4 py-4">
        {turns.map((turn, index) => (
          <div key={`${turn.role}-${index}-${turn.ts ?? index}`} className={turn.role === "user" ? "text-right" : ""}>
            <div
              className={`inline-block max-w-[92%] rounded-xl px-3.5 py-2.5 text-left text-sm leading-7 ${
                turn.role === "user"
                  ? "rounded-br-sm border border-accent-line bg-accent-soft"
                  : "rounded-bl-sm bg-subtle"
              }`}
            >
              <Markdown text={turn.text} />
            </div>

            {turn.role === "assistant" && (
              <div className="mt-1.5 space-y-1.5 text-xs text-muted">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span>{turn.provider?.model || "原模型未记录"}{turn.provider?.host ? ` · ${turn.provider.host}` : ""}{turn.output_language ? ` · ${turn.output_language === "zh-CN" ? "简体中文" : "English"}` : ""}{turn.ts ? ` · ${new Date(turn.ts * 1000).toLocaleString()}` : ""}</span>
                  <button type="button" onClick={() => void copyReply(turn.text, index)} className="quiet-button -my-1 text-muted">
                    <Icon name={copiedIndex === index ? "check" : "copy"} size={12} />{copiedIndex === index ? "已复制" : "复制回答"}
                  </button>
                </div>
                {(turn.partial || (turn.status && turn.status !== "ok")) && <p className="text-warn" role="status">本轮未完整交付：{turn.stopped_reason || "回答中断"}。可以重新发送问题继续询问。</p>}
                <div className="flex flex-wrap items-center gap-1">
                  <span>{turn.tools_used && turn.tools_used.length > 0 ? "这次查了：" : "这次没有查（凭已有产物回答）"}</span>
                  {(turn.tools_used ?? []).map((tool, toolIndex) => (
                    <span key={`${tool}-${toolIndex}`} className="rounded-md border border-line px-1.5 py-px">
                      {toolLabel(tool)}
                    </span>
                  ))}
                </div>
                {(turn.citations ?? []).length > 0 && (
                  <div className="flex flex-wrap items-center gap-1">
                    <span>回答里的代码位置：</span>
                    {(turn.citations ?? []).map((citation, citationIndex) => (
                      <button
                        key={`${citation.path}-${citationIndex}`}
                        type="button"
                        onClick={() => onOpenCitation?.(citation.path, citation.line_start, citation.line_end)}
                        title={citation.verified ? "已在那个 commit 上核对过存在" : citation.reason}
                        data-tone={citation.verified ? "ok" : "fail"}
                        className="badge font-mono hover:underline"
                      >
                        {citation.path}:{citation.line_start}
                        {citation.line_end !== citation.line_start ? `-${citation.line_end}` : ""}
                        <span className="sr-only">{citation.verified ? "（已核对）" : "（未通过核对）"}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        ))}

        {streaming && (
          <div>
            <div className="inline-block max-w-[92%] rounded-xl rounded-bl-sm bg-subtle px-3.5 py-2.5 text-sm leading-7">
              <Markdown text={streaming} />
            </div>
            <p className="mt-1.5 text-xs text-muted">正在回答…</p>
          </div>
        )}
      </div>

      <div className="border-t border-line px-4 py-3">
        <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
          <span className="min-w-0 break-words text-accent">
            {context ? <>针对 <Markdown inline text={context.name} /></> : "针对整篇论文"}
          </span>
          {(context || canFocusInnovation) && (
            <button type="button" onClick={onClearContext} className="quiet-button">
              {context ? "改问整篇论文" : "回到当前创新点"}
            </button>
          )}
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
          <textarea
            aria-label="追问内容"
            aria-describedby="chat-input-help"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                submit();
              }
            }}
            rows={2}
            placeholder="例如：这个 scaling 在哪里用到？"
            className="field min-h-[4.5rem] flex-1 resize-y"
          />
          <button
            type="button"
            onClick={submit}
            disabled={busy || disabled || !draft.trim()}
            className="primary-button sm:min-w-24"
          >
            {busy ? "回答中…" : "发送"}
          </button>
        </div>
        <p id="chat-input-help" className="mt-2 text-xs text-muted">{disabled ? "分析进行中，可先写好问题，结束后发送。" : "Enter 发送 · Shift+Enter 换行"}</p>
      </div>
    </section>
  );
}
