"use client";

import { useEffect, useRef, useState } from "react";

import Markdown from "@/components/Markdown";

export interface ChatCitation {
  path: string;
  line_start: number;
  line_end: number;
  verified: boolean;
  reason?: string;
}

export interface ChatTurn {
  role: "user" | "assistant";
  text: string;
  ts?: number;
  tools_used?: string[];
  citations?: ChatCitation[];
  stopped_reason?: string;
}

interface Props {
  turns: ChatTurn[];
  /** 正在流式输出的那一轮回答（还没落盘） */
  streaming: string;
  busy: boolean;
  context: { id: string; name: string } | null;
  onClearContext: () => void;
  onSend: (message: string) => void;
  disabled?: boolean;
  /** 点引用 → 让上方阅读器跳过去 */
  onOpenCitation?: (path: string, start: number, end: number) => void;
}

const TOOL_LABELS: Record<string, string> = {
  list_pages: "列页",
  get_page_text: "读论文页",
  search_paper: "搜论文",
  read_paper_all: "读全文",
  repo_tree: "看目录",
  search_code: "搜代码",
  read_file: "读代码",
  finish: "结束",
};

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
  context,
  onClearContext,
  onSend,
  disabled,
  onOpenCitation,
}: Props) {
  const [draft, setDraft] = useState("");
  const scrollerRef = useRef<HTMLDivElement>(null);

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
    <section className="rounded-lg border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-950">
      <header className="flex flex-wrap items-center gap-2 border-b border-neutral-200 px-3 py-2 dark:border-neutral-800">
        <h2 className="text-sm font-semibold">7. 追问</h2>
        <span className="text-[11px] text-neutral-500">
          它可以自己去翻论文和代码（每条消息最多 8 次工具调用），回答里的代码位置会被机械核对
        </span>
      </header>

      <div ref={scrollerRef} className="max-h-[52vh] min-h-[160px] space-y-3 overflow-auto p-3">
        {turns.length === 0 && !streaming && (
          <p className="text-sm text-neutral-500">
            还没问过。已经帮你把整份分析结果（创新点、论文证据、代码引用、解释）放在它的上下文里，
            所以可以直接问"这个 scaling 到底在哪用到的""为什么 B 要置零""第 3 页那个公式里的 r 是什么"。
          </p>
        )}

        {turns.map((turn, index) => (
          <div key={`${turn.role}-${index}-${turn.ts ?? index}`} className={turn.role === "user" ? "text-right" : ""}>
            <div
              className={`inline-block max-w-[92%] rounded-lg px-3 py-2 text-left text-sm ${
                turn.role === "user"
                  ? "bg-neutral-900 text-white dark:bg-neutral-100 dark:text-neutral-900"
                  : "bg-neutral-100 dark:bg-neutral-900"
              }`}
            >
              <Markdown text={turn.text} />
            </div>

            {turn.role === "assistant" && (
              <div className="mt-1 space-y-1 text-[11px] text-neutral-500">
                <div className="flex flex-wrap items-center gap-1">
                  <span>{turn.tools_used && turn.tools_used.length > 0 ? "这次查了：" : "这次没有查（凭已有产物回答）"}</span>
                  {(turn.tools_used ?? []).map((tool, toolIndex) => (
                    <span key={`${tool}-${toolIndex}`} className="rounded bg-neutral-100 px-1.5 py-0.5 font-mono dark:bg-neutral-800">
                      {TOOL_LABELS[tool] ?? tool}
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
                        className={`rounded px-1.5 py-0.5 font-mono ${
                          citation.verified
                            ? "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-200"
                            : "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200"
                        }`}
                      >
                        {citation.verified ? "✅" : "⚠"} {citation.path}:{citation.line_start}
                        {citation.line_end !== citation.line_start ? `-${citation.line_end}` : ""}
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
            <div className="inline-block max-w-[92%] rounded-lg bg-neutral-100 px-3 py-2 text-sm dark:bg-neutral-900">
              <Markdown text={streaming} />
            </div>
            <p className="mt-1 text-[11px] text-neutral-500">正在回答…</p>
          </div>
        )}
      </div>

      <div className="border-t border-neutral-200 p-3 dark:border-neutral-800">
        {context && (
          <div className="mb-2 flex items-center gap-2 text-[11px]">
            <span className="rounded bg-blue-100 px-1.5 py-0.5 text-blue-800 dark:bg-blue-950 dark:text-blue-200">
              针对 {context.id} · {context.name}
            </span>
            <button type="button" onClick={onClearContext} className="text-neutral-500 underline">
              取消
            </button>
          </div>
        )}
        <div className="flex gap-2">
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                submit();
              }
            }}
            rows={2}
            placeholder={disabled ? "先上传论文" : "问点什么…（Enter 发送，Shift+Enter 换行）"}
            className="min-h-[52px] flex-1 resize-y rounded border border-neutral-300 bg-transparent px-2 py-1 text-sm dark:border-neutral-700"
          />
          <button
            type="button"
            onClick={submit}
            disabled={busy || disabled || !draft.trim()}
            className="rounded bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40 dark:bg-neutral-100 dark:text-neutral-900"
          >
            {busy ? "回答中…" : "发送"}
          </button>
        </div>
      </div>
    </section>
  );
}
