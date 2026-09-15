"""追问对话（阶段③）。

三件事在这里落地：
1. **对话历史落盘**（`chat.jsonl`）：刷新页面能恢复，下一轮也能带上上下文。
2. **回答里的代码位置要机械核对**：回答是自由文本，但里面写的 `文件:行号` 会被逐条拿去
   git 对象里查——文件在不在、行号超没超范围。核对不过的会被标出来给用户看。
   这样"追问"不会变成一个新幻觉温床。
3. 上下文组装：把已有的分析产物（创新点 + 论文证据 + 代码引用 + 解释）交给对话 Agent，
   让它优先基于产物回答，需要核实时**自己调工具去查**。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import store
from .repo_source import RepoError, RepoSource, text_lines

# 回答里出现的 "路径:行号" / "路径:起-止"
_CITATION = re.compile(
    r"([A-Za-z0-9_][A-Za-z0-9_./\-]*\.(?:py|pyi|md|txt|json|ya?ml|toml|cfg|ini|js|jsx|ts|tsx|c|cc|cpp|h|hpp|cu|cuh|go|rs|java|sh))"
    r"(?::(\d+)(?:\s*[-–~]\s*(\d+))?)?"
)

MAX_TRANSCRIPT_TURNS = 12  # 带进上下文的历史轮数（再往前的就不发了，控制 token）


def transcript_path(run_id: str) -> Path:
    return store.run_dir(run_id) / "chat.jsonl"


def load_transcript(run_id: str) -> list[dict[str, Any]]:
    path = transcript_path(run_id)
    if not path.exists():
        return []
    turns: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            turns.append(json.loads(line))
    return turns


def append_turn(run_id: str, role: str, text: str, **extra: Any) -> dict[str, Any]:
    turn = {"role": role, "text": text, "ts": time.time(), **extra}
    with transcript_path(run_id).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(turn, ensure_ascii=False, default=str) + "\n")
    return turn


def verify_citations(repo: RepoSource | None, text: str) -> list[dict[str, Any]]:
    """把回答里提到的每个代码位置拿去 git 对象里核对一遍。

    只核对**带行号**的引用：光提文件名没法判断对错，标了行号才能查。
    """
    if repo is None or not text:
        return []
    seen: set[tuple[str, int, int]] = set()
    results: list[dict[str, Any]] = []
    for match in _CITATION.finditer(text):
        path, start_raw, end_raw = match.group(1), match.group(2), match.group(3)
        if not start_raw:
            continue
        start = int(start_raw)
        end = int(end_raw) if end_raw else start
        if end < start:
            start, end = end, start
        key = (path, start, end)
        if key in seen:
            continue
        seen.add(key)
        try:
            content = repo.content_at_commit(path)
        except RepoError as exc:
            results.append(
                {
                    "path": path,
                    "line_start": start,
                    "line_end": end,
                    "verified": False,
                    "reason": str(exc)[:160],
                }
            )
            continue
        total = len(text_lines(content))
        ok = 1 <= start <= end <= total
        results.append(
            {
                "path": path,
                "line_start": start,
                "line_end": end,
                "verified": ok,
                "reason": "" if ok else f"行号超出范围（该文件在这个 commit 上共 {total} 行）",
            }
        )
    return results


def build_context_block(artifact: dict[str, Any] | None, context_ids: list[str] | None = None) -> str:
    """把已有分析产物压成一段上下文。用户指定了 context_ids 就把那几条完整展开。"""
    if not artifact:
        return "（这个 run 还没有任何分析产物：用户可能还没跑阶段 A/B。）"

    wanted = {item for item in (context_ids or []) if item}
    lines: list[str] = []
    run_block = artifact.get("run") or {}
    paper = run_block.get("paper") or {}
    repo = run_block.get("repo") or {}
    lines.append(f"论文：{paper.get('title_guess') or '（无标题）'}，共 {paper.get('page_count')} 页")
    if repo:
        lines.append(f"仓库：{repo.get('url')}，锁定 commit {str(repo.get('commit_sha'))[:12]}…")
    verification = artifact.get("verification") or {}
    if verification:
        lines.append(
            f"引用核验率：{verification.get('citation_verifiable_rate')}"
            f"（{verification.get('citations_verified')}/{verification.get('citations_total')} 条通过）"
        )

    for item in artifact.get("innovations", []):
        focus = item.get("id") in wanted
        lines.append("")
        lines.append(f"### {item.get('id')} · {item.get('name')}（status={item.get('status')}，置信度 {item.get('confidence')}）")
        if item.get("one_liner"):
            lines.append(f"一句话：{item['one_liner']}")
        for evidence in item.get("paper_evidence", []) or []:
            lines.append(f"论文证据（第 {evidence.get('page')} 页，核验={evidence.get('verified')}）：“{evidence.get('quote')}”")
        for evidence in item.get("code_evidence", []) or []:
            verified = (evidence.get("verification") or {}).get("state")
            lines.append(
                f"代码引用：{evidence.get('path')}:{evidence.get('line_start')}-{evidence.get('line_end')}"
                f"（{evidence.get('symbol') or '未注明符号'}，核验={verified}）—— {evidence.get('why')}"
            )
        if item.get("status") == "not_found":
            lines.append(f"未找到的理由：{item.get('not_found_reason')}；搜过：{item.get('searched')}")
        explanation = item.get("explanation") or {}
        if explanation.get("intuition"):
            lines.append(f"已有解释（直觉）：{explanation['intuition']}")
        if focus:
            for step in explanation.get("code_walkthrough") or []:
                lines.append(f"  逐段讲解：{step.get('line_ref')} → {step.get('text')}")
            for pitfall in explanation.get("pitfalls") or []:
                lines.append(f"  已记录的常见误解：{pitfall}")
            if explanation.get("read_next"):
                lines.append(f"  建议接下来读：{explanation['read_next']}")
    return "\n".join(lines)


def build_chat_prompt(transcript: list[dict[str, Any]], question: str, context_ids: list[str] | None = None) -> str:
    history = transcript[-MAX_TRANSCRIPT_TURNS:]
    chunks: list[str] = []
    if history:
        chunks.append("## 之前的对话")
        for turn in history:
            speaker = "用户" if turn.get("role") == "user" else "你"
            chunks.append(f"{speaker}：{turn.get('text')}")
    if context_ids:
        chunks.append("")
        chunks.append(f"（用户正在追问这些条目：{', '.join(context_ids)}）")
    chunks.append("")
    chunks.append(f"## 这次的问题\n{question}")
    return "\n".join(chunks)


__all__ = [
    "load_transcript",
    "append_turn",
    "verify_citations",
    "build_context_block",
    "build_chat_prompt",
    "transcript_path",
]
