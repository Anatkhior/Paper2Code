"use client";

import { useRef, useState, type KeyboardEvent } from "react";
import { FindingStatus, PaperEvidenceList } from "@/components/Evidence";
import type { Finding, Innovation, Plan } from "@/lib/types";

interface Props {
  plan: Plan;
  selected: Set<string>;
  activeId: string;
  findings: Finding[];
  missingIds: string[];
  busy: boolean;
  onActivate: (id: string) => void;
  onToggle: (id: string) => void;
  onSelectAll: () => void;
}

interface DetailProps {
  active: Innovation;
  hasFinding: boolean;
  onRename: (id: string, name: string) => void;
  onDelete: (id: string) => void;
  onOpenPaper: (page: number, quote: string) => void;
}

const DIFFICULTY: Record<Innovation["difficulty"], string> = {
  beginner: "入门", medium: "中等", hard: "偏难",
};

export default function PlanList({ plan, selected, activeId, findings, missingIds, busy, onActivate, onToggle, onSelectAll }: Props) {
  const tabsRef = useRef<HTMLDivElement>(null);
  const innovations = plan.innovations;
  const selectedCount = innovations.filter((item) => selected.has(item.id)).length;

  const navigateTab = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next: number;
    switch (event.key) {
      case "ArrowDown": case "ArrowRight": next = (index + 1) % innovations.length; break;
      case "ArrowUp": case "ArrowLeft": next = (index - 1 + innovations.length) % innovations.length; break;
      case "Home": next = 0; break;
      case "End": next = innovations.length - 1; break;
      default: return;
    }
    event.preventDefault();
    onActivate(innovations[next].id);
    const button = tabsRef.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next];
    button?.focus({ preventScroll: true });
    if (button) {
      const bounds = button.getBoundingClientRect();
      const topInset = 80; // 顶部常驻状态条不能盖住键盘焦点。
      if (bounds.top < topInset) window.scrollBy(0, bounds.top - topInset);
      else if (bounds.bottom > window.innerHeight) window.scrollBy(0, bounds.bottom - window.innerHeight);
    }
  };

  return (
    <nav className="innovation-directory" aria-label="创新点目录">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs text-neutral-500">已选 {selectedCount} / {innovations.length} 条</span>
        <button type="button" disabled={busy || !innovations.length} onClick={onSelectAll} className="text-xs text-teal-700 disabled:opacity-40 dark:text-teal-300">
          {innovations.length > 0 && selectedCount === innovations.length ? "取消全选" : "全选"}
        </button>
      </div>
      <div ref={tabsRef} className="innovation-tabs" role="tablist" aria-label="核心创新点" aria-orientation="vertical">
        {innovations.map((item, index) => {
          const finding = findings.find((entry) => entry.id === item.id);
          return (
            <div key={item.id} role="presentation" className="innovation-nav-item">
              <input type="checkbox" checked={selected.has(item.id)} disabled={busy} onChange={() => onToggle(item.id)} aria-label={`定位「${item.name}」`} className="innovation-check" />
              <button type="button" id={`innovation-tab-${item.id}`} role="tab" tabIndex={activeId === item.id ? 0 : -1} aria-selected={activeId === item.id} aria-controls="innovation-detail" onClick={() => onActivate(item.id)} onKeyDown={(event) => navigateTab(event, index)} title={item.name} className="innovation-tab">
                <span className="mb-1 flex flex-wrap items-center gap-2 text-[11px] text-neutral-500">
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  {finding ? <FindingStatus status={finding.status} /> : <span>{missingIds.includes(item.id) ? "未产出结论" : busy && selected.has(item.id) ? "定位中" : "待定位"}</span>}
                  {item.source === "user" && <span>你添加的</span>}
                </span>
                <span className="innovation-tab-title">{item.name}</span>
              </button>
            </div>
          );
        })}
      </div>
      {!innovations.length && <p className="text-sm text-neutral-500">清单里还没有创新点。可在原文文本中划选添加目标。</p>}
      <p className="mt-3 text-[11px] text-neutral-500">点标题阅读 · 勾选后定位</p>
    </nav>
  );
}

// 状态随条目身份重建，切换或删除当前条目时不会把草稿、展开状态带到下一条。
export function PlanDetail({ active, onRename, onDelete, onOpenPaper, hasFinding }: DetailProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const difficulty = DIFFICULTY[active.difficulty];

  return (
    <div className="plan-detail">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        {editing ? (
          // 改名框占满卡片内宽（w-full 换行独占一行）：曾经内联在标题行里，
          // 长名字会把框撑出卡片边界，超出的部分被相邻卡片的背景盖住（用户实测反馈）
          <input
            autoFocus
            aria-label="创新点名称"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onBlur={() => {
              if (draft.trim() && draft.trim() !== active.name) onRename(active.id, draft.trim());
              setEditing(false);
            }}
            onKeyDown={(event) => {
              if (event.nativeEvent.isComposing) return;
              if (event.key === "Enter") event.currentTarget.blur();
              if (event.key === "Escape") setEditing(false);
            }}
            maxLength={120}
            className="w-full rounded border border-neutral-300 px-1.5 py-0.5 text-sm dark:border-neutral-700"
          />
        ) : (
          <h3 className="plan-detail-title text-xl font-semibold tracking-tight">{active.name}</h3>
        )}
        {active.source === "user" && (
          <span className="rounded bg-blue-100 px-1.5 py-0.5 text-[11px] text-blue-800 dark:bg-blue-950 dark:text-blue-200">
            你添加的
          </span>
        )}
        <span className="rounded bg-neutral-100 px-1.5 py-0.5 text-[11px] text-neutral-500 dark:bg-neutral-800">
          {difficulty}
        </span>
        {!editing && (
          <button
            type="button"
            onClick={() => {
              setDraft(active.name);
              setEditing(true);
            }}
            className="text-[11px] text-neutral-400 underline"
          >
            改名
          </button>
        )}
        <button
            type="button"
            onClick={() => onDelete(active.id)}
            className="text-[11px] text-red-500 underline"
          >
            删除
          </button>
      </div>

      <p className="mt-3 text-sm leading-relaxed text-neutral-700 dark:text-neutral-300">{active.one_liner}</p>

      {!hasFinding && <div className="mt-4">
        <button type="button" onClick={() => setEvidenceOpen((current) => !current)} aria-expanded={evidenceOpen} aria-controls={`plan-evidence-${active.id}`} className="text-xs text-teal-700 dark:text-teal-300">
          {evidenceOpen ? "收起引文与关键词 ↑" : `展开引文与关键词（${active.paper_evidence.length} 条引文） ↓`}
        </button>
        <div id={`plan-evidence-${active.id}`} hidden={!evidenceOpen} className="mt-4 space-y-3">
          <PaperEvidenceList evidence={active.paper_evidence} onOpenPaper={onOpenPaper} />
          {active.search_hints.length > 0 && (
            <div className="flex flex-wrap gap-1 pt-1">
              {active.search_hints.map((hint) => (
                <span
                  key={hint}
                  className="rounded bg-neutral-100 px-1.5 py-0.5 font-mono text-[11px] text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300"
                >
                  {hint}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>}
    </div>
  );
}
