"""用户自定义的定位目标（阶段 C：我自己指定想看什么）。

设计取舍：
- **清单是可变状态，存在 `plan.json` 里的同一份 `plan` 对象上**。
  阶段 B 的 locate 读的就是它，所以用户加的目标天然会被定位，不需要额外通路。
- 用户加的条目 id 形如 `user-1`，与 Agent 生成的 `inn-1` 区分开，界面上也好标记。
- 用户没跑侦察也能加目标：他就是想指定"我想看懂这一段"，不该被流程拦住。
- 用户选的原文片段**同样走引文核验**（quote_found）。核验不通过不拒绝用户输入，
  只标记 `verified=false` —— 用户可能手打了一段描述，而不是逐字引用。
"""

from __future__ import annotations

import re
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import store
from .paper import PaperDocument

# 从用户选中的原文里猜"代码里可能出现的标识符"，给定位阶段当搜索线索。
# 这只是一个**可编辑的默认值**：用户随时能改，Agent 也不会盲信它。
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_\-.]{3,}")
_STOPWORDS = {
    "the", "and", "with", "that", "this", "from", "have", "which", "were", "been", "their",
    "there", "into", "than", "then", "them", "these", "those", "such", "using", "used", "when",
    "where", "while", "would", "could", "should", "about", "after", "before", "between",
    "during", "because", "paper", "method", "methods", "results", "figure", "table", "section",
    "approach", "training", "model", "models", "parameters", "value", "values", "given",
}


def derive_hints(text: str, limit: int = 6) -> list[str]:
    """从用户选中的原文里挑出"看起来像代码标识符"的词，作为默认搜索线索。

    刻意只保留形如 `lora_alpha` / `self.scaling` / `LoRALayer` / `dW` 这种词：
    普通英文单词（write、shape、training）当搜索线索只会误导。
    挑不出来就返回空——**宁可留空让 Agent 自己判断要搜什么**，也不要塞一堆废词。
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in _TOKEN.findall(text or ""):
        # 句末的句号/连字符会被正则带进来（"magnitude."），先剥掉
        token = raw.rstrip(".-")
        if not token or token.lower() in _STOPWORDS:
            continue
        looks_like_code = (
            "_" in token                      # lora_alpha
            or "." in token                   # self.scaling（剥掉尾部句号后，剩下的点一定是内部的）
            or any(char.isdigit() for char in token)      # w2
            or any(char.isupper() for char in token[1:])  # LoRALayer / dW
        )
        if not looks_like_code:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
        if len(out) >= limit:
            break
    return out


class PlanItemIn(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    one_liner: str | None = Field(default=None, max_length=400)
    page: int | None = Field(default=None, ge=1)
    quote: str | None = Field(default=None, max_length=1200)
    search_hints: list[str] = Field(default_factory=list)
    difficulty: Literal["beginner", "medium", "hard"] = "beginner"


class PlanItemPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    one_liner: str | None = Field(default=None, max_length=400)
    search_hints: list[str] | None = None


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def load_plan(run_id: str) -> dict[str, Any] | None:
    summary = store.read_json(store.run_dir(run_id) / "plan.json") or {}
    return summary.get("plan")


def save_plan(run_id: str, plan: dict[str, Any]) -> None:
    """把清单写回 plan.json，保留原有的 run 摘要字段。"""
    path = store.run_dir(run_id) / "plan.json"
    summary = store.read_json(path) or {}
    summary["plan"] = plan
    summary.setdefault("status", "user_defined")
    summary.setdefault("stopped_reason", "用户自定义目标")
    store.write_json(path, summary)


def _empty_plan() -> dict[str, Any]:
    return {
        "paper_summary": "（这些目标由你自己指定，没有经过侦察阶段）",
        "coverage_note": "用户手动指定的定位目标",
        "innovations": [],
    }


def next_user_id(plan: dict[str, Any]) -> str:
    used = {
        int(match.group(1))
        for item in plan.get("innovations", [])
        if (match := re.fullmatch(r"user-(\d+)", str(item.get("id", ""))))
    }
    return f"user-{(max(used) + 1) if used else 1}"


def _open_paper(run_id: str) -> PaperDocument | None:
    pdf = store.paper_path(run_id)
    if not pdf.exists():
        return None
    return PaperDocument(pdf, cache_dir=store.run_dir(run_id) / "cache")


def add_item(run_id: str, payload: PlanItemIn) -> dict[str, Any]:
    plan = load_plan(run_id) or _empty_plan()
    quote = (payload.quote or "").strip()
    name = (payload.name or "").strip() or (quote[:24] if quote else "（未命名目标）")
    one_liner = (payload.one_liner or "").strip() or (quote[:80] + ("…" if len(quote) > 80 else "") if quote else "")

    evidence: list[dict[str, Any]] = []
    if quote:
        match: str | None = None
        paper = _open_paper(run_id)
        if paper is not None and payload.page:
            try:
                match = paper.quote_match(payload.page, quote)
            finally:
                paper.close()
        evidence.append(
            {
                "page": payload.page or 1,
                "quote": quote,
                "kind": "text",
                # verified=False 不拒绝用户输入，只如实标记；partial 同样标出来
                "verified": match in {"full", "partial"} if match is not None else None,
                "quote_match": match,
            }
        )

    hints = [hint for hint in payload.search_hints if hint.strip()] or derive_hints(quote) or derive_hints(name)

    item = {
        "id": next_user_id(plan),
        "name": name,
        "one_liner": one_liner,
        "difficulty": payload.difficulty,
        "paper_evidence": evidence,
        "search_hints": hints,
        "source": "user",
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    plan.setdefault("innovations", []).append(item)
    save_plan(run_id, plan)
    return plan


def update_item(run_id: str, item_id: str, patch: PlanItemPatch) -> dict[str, Any]:
    plan = load_plan(run_id)
    if not plan:
        raise KeyError(item_id)
    for item in plan.get("innovations", []):
        if item.get("id") == item_id:
            if patch.name is not None:
                item["name"] = patch.name
            if patch.one_liner is not None:
                item["one_liner"] = patch.one_liner
            if patch.search_hints is not None:
                item["search_hints"] = [hint for hint in patch.search_hints if hint.strip()]
            save_plan(run_id, plan)
            return plan
    raise KeyError(item_id)


def delete_item(run_id: str, item_id: str) -> dict[str, Any]:
    plan = load_plan(run_id)
    if not plan:
        raise KeyError(item_id)
    innovations = plan.get("innovations", [])
    remaining = [item for item in innovations if item.get("id") != item_id]
    if len(remaining) == len(innovations):
        raise KeyError(item_id)
    plan["innovations"] = remaining
    save_plan(run_id, plan)
    return plan


__all__ = [
    "PlanItemIn",
    "PlanItemPatch",
    "add_item",
    "update_item",
    "delete_item",
    "load_plan",
    "save_plan",
    "derive_hints",
    "next_user_id",
]
