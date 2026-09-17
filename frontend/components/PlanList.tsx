"use client";

import { useState } from "react";

import type { Innovation, Plan } from "@/lib/types";

interface Props {
  plan: Plan | null;
  selected: Set<string>;
  onToggle: (id: string) => void;
  onSelectAll: () => void;
  onRename?: (id: string, name: string) => void;
  onDelete?: (id: string) => void;
}

const DIFFICULTY: Record<Innovation["difficulty"], { label: string; className: string }> = {
  beginner: { label: "入门", className: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-200" },
  medium: { label: "中等", className: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200" },
  hard: { label: "偏难", className: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200" },
};

export default function PlanList({
  plan,
  selected,
  onToggle,
  onSelectAll,
  onRename,
  onDelete,
}: Props) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  if (!plan) {
    return (
      <div className="rounded-lg border border-dashed border-neutral-300 p-6 text-sm text-neutral-500 dark:border-neutral-800">
        阶段 A 完成后，这里会列出论文的核心创新点，由你勾选要深入定位哪几条。
        <p className="mt-2 text-xs">
          为什么要你勾选：系统猜的核心创新点未必是<strong>你想看懂的那一块</strong>。
          你也可以直接在左边原文里划选一段，把它加成「你添加的」目标，让系统照着你指的方向去找。
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-[11px] text-neutral-500">
        清单里带「你添加的」标记的条目，是你在左边原文里划选加进来的——系统照着它去找代码，而不是照 Agent 的猜测。
      </p>

      <div className="rounded-lg border border-neutral-200 bg-white p-3 text-sm dark:border-neutral-800 dark:bg-neutral-950">
        <p className="font-medium">论文摘要（Agent 的概括）</p>
        <p className="mt-1 whitespace-pre-wrap text-neutral-700 dark:text-neutral-300">{plan.paper_summary}</p>
        {plan.coverage_note && (
          <p className="mt-2 text-xs text-neutral-500">覆盖说明：{plan.coverage_note}</p>
        )}
      </div>

      <div className="flex items-center justify-between">
        <p className="text-sm font-semibold">
          核心创新点（{plan.innovations.length} 条，已选 {selected.size} 条）
        </p>
        <button type="button" onClick={onSelectAll} className="text-xs text-neutral-500 underline">
          全选 / 全不选
        </button>
      </div>

      <div className="plan-list">
      {plan.innovations.map((innovation) => {
        const difficulty = DIFFICULTY[innovation.difficulty] ?? DIFFICULTY.medium;
        return (
          <label
            key={innovation.id}
            className={`block cursor-pointer rounded-lg border p-3 transition ${
              selected.has(innovation.id)
                ? "border-neutral-900 bg-neutral-50 dark:border-neutral-100 dark:bg-neutral-900"
                : "border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-950"
            }`}
          >
            <div className="flex items-start gap-3">
              <input
                type="checkbox"
                checked={selected.has(innovation.id)}
                onChange={() => onToggle(innovation.id)}
                className="mt-1"
              />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  {editing === innovation.id ? (
                    <input
                      autoFocus
                      value={draft}
                      onChange={(event) => setDraft(event.target.value)}
                      onBlur={() => {
                        if (draft.trim()) onRename?.(innovation.id, draft.trim());
                        setEditing(null);
                      }}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") event.currentTarget.blur();
                        if (event.key === "Escape") setEditing(null);
                      }}
                      className="rounded border border-neutral-300 px-1 py-0.5 text-sm dark:border-neutral-700"
                    />
                  ) : (
                    <span className="font-medium">{innovation.name}</span>
                  )}
                  {innovation.source === "user" && (
                    <span className="rounded bg-blue-100 px-1.5 py-0.5 text-[11px] text-blue-800 dark:bg-blue-950 dark:text-blue-200">
                      你添加的
                    </span>
                  )}
                  <span className={`rounded px-1.5 py-0.5 text-[11px] ${difficulty.className}`}>{difficulty.label}</span>
                  <span className="font-mono text-[11px] text-neutral-400">{innovation.id}</span>
                  {onRename && editing !== innovation.id && (
                    <button
                      type="button"
                      onClick={(event) => {
                        event.preventDefault();
                        setDraft(innovation.name);
                        setEditing(innovation.id);
                      }}
                      className="text-[11px] text-neutral-400 underline"
                    >
                      改名
                    </button>
                  )}
                  {onDelete && (
                    <button
                      type="button"
                      onClick={(event) => {
                        event.preventDefault();
                        onDelete(innovation.id);
                      }}
                      className="text-[11px] text-red-500 underline"
                    >
                      删除
                    </button>
                  )}
                </div>
                <p className="mt-1 text-sm text-neutral-700 dark:text-neutral-300">{innovation.one_liner}</p>

                <div className="mt-2 space-y-1">
                  {innovation.paper_evidence.map((evidence, index) => (
                    <div key={index} className="rounded border border-neutral-200 p-2 text-xs dark:border-neutral-800">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-[11px] text-neutral-500">第 {evidence.page} 页</span>
                        {evidence.verified === true && evidence.quote_match !== "partial" && (
                          <span className="text-[11px] text-green-600">✅ 引文已核验</span>
                        )}
                        {evidence.verified === true && evidence.quote_match === "partial" && (
                          <span
                            className="text-[11px] text-amber-600"
                            title="只有开头部分逐字匹配到了这一页，后面的内容没有逐字出现——警惕后半段是编的"
                          >
                            ◐ 引文部分匹配
                          </span>
                        )}
                        {evidence.verified === false && <span className="text-[11px] text-red-600">⚠ 引文未通过核验</span>}
                        {evidence.verified == null && <span className="text-[11px] text-neutral-400">未核验</span>}
                      </div>
                      <p className="mt-1 whitespace-pre-wrap text-neutral-700 dark:text-neutral-300">“{evidence.quote}”</p>
                    </div>
                  ))}
                </div>

                {innovation.search_hints.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {innovation.search_hints.map((hint) => (
                      <span key={hint} className="rounded bg-neutral-100 px-1.5 py-0.5 font-mono text-[11px] text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
                        {hint}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </label>
        );
      })}

      </div>

      {/* 这里原本还有一个「开始定位选中项」按钮，与第 4 节「代码仓库」里的按钮重复 →
          已移除（定位需要先填仓库地址，动作应该只出现在填地址的那一节旁边）。 */}
      <p className="text-[11px] text-neutral-500">
        阶段 B：对每条选中的创新点，克隆仓库并让 Agent 自主探索，产出带 commit/文件/行号/片段哈希的代码引用，
        再由后端从 git 对象里重放核验。找得到就给对照解读，找不到就明确写「未找到」。
      </p>
    </div>
  );
}
