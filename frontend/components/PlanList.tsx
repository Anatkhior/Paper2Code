"use client";

import { useRef, useState, type KeyboardEvent } from "react";
import { FindingStatus, PaperEvidenceList } from "@/components/Evidence";
import Icon from "@/components/Icon";
import Markdown from "@/components/Markdown";
import type { Finding, Innovation, Plan } from "@/lib/types";

interface Props {
  readOnly?: boolean;
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
  readOnly?: boolean;
  active: Innovation;
  hasFinding: boolean;
  onRename: (id: string, name: string) => void;
  onDelete: (id: string) => void;
  onOpenPaper: (page: number, quote: string) => void;
}

const DIFFICULTY: Record<Innovation["difficulty"], string> = {
  beginner: "入门", medium: "中等", hard: "偏难",
};

export default function PlanList({ plan, selected, activeId, findings, missingIds, busy, onActivate, onToggle, onSelectAll, readOnly }: Props) {
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
      {!readOnly && <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs tabular-nums text-muted">已选 {selectedCount} / {innovations.length} 条</span>
        <button type="button" disabled={busy || !innovations.length} onClick={onSelectAll} className="quiet-button -mr-2">
          {innovations.length > 0 && selectedCount === innovations.length ? "取消全选" : "全选"}
        </button>
      </div>}
      <div ref={tabsRef} className="innovation-tabs" role="tablist" aria-label="核心创新点" aria-orientation="vertical">
        {innovations.map((item, index) => {
          const finding = findings.find((entry) => entry.id === item.id);
          return (
            <div key={item.id} role="presentation" className="innovation-nav-item" data-readonly={readOnly ? "" : undefined}>
              {!readOnly && <input type="checkbox" checked={selected.has(item.id)} disabled={busy} onChange={() => onToggle(item.id)} aria-label={`定位「${item.name}」`} className="innovation-check" />}
              <button type="button" id={`innovation-tab-${item.id}`} role="tab" tabIndex={activeId === item.id ? 0 : -1} aria-selected={activeId === item.id} aria-controls="innovation-detail" onClick={() => onActivate(item.id)} onKeyDown={(event) => navigateTab(event, index)} title={item.name} className="innovation-tab">
                <span className="mb-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-muted">
                  <span className="tabular-nums">{String(index + 1).padStart(2, "0")}</span>
                  {finding ? <FindingStatus plain status={finding.status} /> : missingIds.includes(item.id) ? <span data-tone="warn" className="status-label">未产出结论</span> : busy && selected.has(item.id) ? <span data-tone="run" className="status-label">定位中</span> : <span>待定位</span>}
                  {item.source === "user" && <span>你添加的</span>}
                </span>
                <span className="innovation-tab-title"><Markdown inline text={item.name} /></span>
              </button>
            </div>
          );
        })}
      </div>
      {!innovations.length && <p className="text-sm leading-6 text-muted">{readOnly ? "这份历史尚未保存创新点。" : "清单里还没有创新点。可在原文文本中划选添加目标。"}</p>}
      <p className="mt-3 text-xs text-muted">{readOnly ? "点标题阅读历史结论" : "点标题阅读 · 勾选后定位"}</p>
    </nav>
  );
}

// 状态随条目身份重建，切换或删除当前条目时不会把草稿、展开状态带到下一条。
export function PlanDetail({ active, onRename, onDelete, onOpenPaper, hasFinding, readOnly }: DetailProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const difficulty = DIFFICULTY[active.difficulty];

  return (
    <div className="plan-detail">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-2">
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
            className="field text-base"
          />
        ) : (
          <h3 className="plan-detail-title text-xl font-semibold leading-snug tracking-tight text-balance"><Markdown inline text={active.name} /></h3>
        )}
        {active.source === "user" && (
          <span data-tone="info" className="badge">你添加的</span>
        )}
        <span className="rounded-full border border-line px-2 py-px text-xs leading-5 text-muted">难度 · {difficulty}</span>
        {!readOnly && <span className="ml-auto flex items-center gap-1">
          {!editing && <button
            type="button"
            onClick={() => {
              setDraft(active.name);
              setEditing(true);
            }}
            className="quiet-button text-muted"
          >
            <Icon name="pencil" size={14} />改名
          </button>}
          {confirming ? <>
            <button type="button" autoFocus onClick={() => onDelete(active.id)} onBlur={(event) => { if (!event.currentTarget.parentElement?.contains(event.relatedTarget)) setConfirming(false); }} className="danger-button">确认删除</button>
            <button type="button" onClick={() => setConfirming(false)} className="quiet-button text-muted">保留</button>
          </> : <button type="button" onClick={() => setConfirming(true)} className="quiet-button text-muted hover:text-fail">
            <Icon name="trash" size={14} />删除
          </button>}
        </span>}
      </div>

      <Markdown text={active.one_liner} className="mt-3 max-w-[72ch] text-[0.9375rem] leading-7 text-foreground/85" />

      {!hasFinding && <div className="mt-4">
        <button type="button" onClick={() => setEvidenceOpen((current) => !current)} aria-expanded={evidenceOpen} aria-controls={`plan-evidence-${active.id}`} className="quiet-button -ml-2">
          {evidenceOpen ? "收起引文与关键词 ↑" : `展开引文与关键词（${active.paper_evidence.length} 条引文） ↓`}
        </button>
        <div id={`plan-evidence-${active.id}`} hidden={!evidenceOpen} className="mt-4 space-y-3">
          <PaperEvidenceList evidence={active.paper_evidence} onOpenPaper={onOpenPaper} />
          {active.search_hints.length > 0 && (
            <div className="flex flex-wrap gap-1 pt-1">
              {active.search_hints.map((hint) => (
                <span
                  key={hint}
                  className="rounded-md border border-line px-1.5 py-0.5 font-mono text-[11px] text-muted"
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
