"""阶段 A 的提交工具：record_plan。

这是"Agent 自主探索"和"结构化交付"缝合的地方（docs/v0-spec.md §6 的落地方式）：
**不让模型在最后吐一个巨大的 JSON**（那样必然解析失败、前端在结束前什么都渲染不出来），
而是让它通过一个工具**结构化地提交**，由后端校验后即时推送给前端。

这个工具做了三件后端该做的事（都是校验，不是决策）：
1. schema 校验（pydantic）
2. 引文核验——它声称的原文是否真的出现在那一页（诚实性机制）
3. 校验失败时把**具体哪一条错了**回给模型，给它一次改正机会，但不允许无限重试
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from ...paper import PaperDocument
from .base import Tool, ToolContext, ToolError, ToolResult, json_content

# 允许被打回的次数：被引文核验打回 1 次之后，再提交就"接受但标记 verified=false"。
# 目的是既给模型改正机会，又不让它无限重试（重试是要花钱的）。
MAX_QUOTE_REJECTIONS = 1
# 解释质量也允许打回一次。"一句话带过"对本项目的用途没有价值（读者要的是能建立起对应关系）。
MAX_QUALITY_REJECTIONS = 1
MIN_INTUITION_CHARS = 40


def _explanation_problems(finding: "Finding") -> list[str]:
    """最低要求。这些不是文风偏好，而是"这段解释有没有信息量"的底线。"""
    problems: list[str] = []
    explanation = finding.explanation
    length = len(explanation.intuition.strip())
    if length < MIN_INTUITION_CHARS:
        problems.append(
            f"explanation.intuition 只有 {length} 字（至少要 {MIN_INTUITION_CHARS} 字）："
            "用生活化的话讲清'它到底在干什么'，不要只给一句术语解释"
        )
    if finding.status in {"matched", "partial"} and not explanation.code_walkthrough:
        problems.append(
            "explanation.code_walkthrough 为空：必须逐段讲解你引用的代码，"
            "每条 line_ref 指向真实行号（例如 'loralib/layers.py:37-43'）"
        )
    if not explanation.pitfalls:
        problems.append(
            "explanation.pitfalls 为空：至少写一条'容易搞错的地方'"
        )
    return problems


class PaperEvidence(BaseModel):
    page: int = Field(ge=1, description="原文所在页码")
    quote: str = Field(min_length=8, description="原文片段（尽量逐字，至少 8 个字符）")
    kind: Literal["text", "equation", "figure", "table"] = "text"


class Innovation(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    one_liner: str = Field(min_length=1)
    difficulty: Literal["beginner", "medium", "hard"] = "medium"
    paper_evidence: list[PaperEvidence] = Field(min_length=1)
    search_hints: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    paper_summary: str = Field(min_length=1)
    innovations: list[Innovation] = Field(min_length=1, max_length=6)
    coverage_note: str = ""


def _validate_plan(raw: dict) -> tuple[Plan | None, str]:
    try:
        return Plan.model_validate(raw), ""
    except ValidationError as exc:
        lines = []
        for error in exc.errors()[:6]:
            location = ".".join(str(part) for part in error["loc"])
            lines.append(f"- {location}: {error['msg']}")
        return None, "\n".join(lines)


async def _record_plan(args: dict, ctx: ToolContext) -> ToolResult:
    # 只有"引文核验不通过"才消耗改正机会。
    # schema 写错（少字段、类型不对）不消耗——那是便宜的笔误，模型基本一次就能改对；
    # 而引文编造是诚信问题，值得用专门的计数管住。
    rejections = int(ctx.state.get("record_plan_quote_rejections", 0))

    plan, problem = _validate_plan(args)
    if plan is None:
        raise ToolError(f"plan 结构不合格，请修正后重新提交：\n{problem}")

    ids = [item.id for item in plan.innovations]
    if len(set(ids)) != len(ids):
        raise ToolError(f"innovation 的 id 必须唯一，当前为 {ids}")

    # --- 引文核验：它说的原文真的在那一页吗？ ---
    paper: PaperDocument | None = ctx.paper
    unverified: list[str] = []
    for innovation in plan.innovations:
        for evidence in innovation.paper_evidence:
            if not (paper.quote_found(evidence.page, evidence.quote) if paper else True):
                unverified.append(f"{innovation.id} 第 {evidence.page} 页：{evidence.quote[:60]}…")

    if unverified and rejections < MAX_QUOTE_REJECTIONS:
        ctx.state["record_plan_quote_rejections"] = rejections + 1
        listing = "\n".join(f"- {item}" for item in unverified)
        raise ToolError(
            f"以下引文在你声称的页码里**找不到**（可能是记错了页码，或者引文不是原文）：\n{listing}\n"
            "请用 get_page_text 重新读那一页，用真正的原文，然后再次调用 record_plan。"
        )

    plan_payload = plan.model_dump()
    # 用户在侦察之前/之后加的自定义目标（source=user）不能被侦察结果冲掉：
    # plan.json 是同一份可变状态，record_plan 提交时把已有的 user 条目并进来。
    # （真实场景：用户上传后先划选两段"我想看懂这里"，再点「开始侦察」。）
    from ...plan import load_plan

    existing_items = (load_plan(ctx.run_id) or {}).get("innovations") or []
    existing_ids = {item["id"] for item in plan_payload["innovations"]}
    for item in existing_items:
        if item.get("source") == "user" and item.get("id") not in existing_ids:
            plan_payload["innovations"].append(item)
            existing_ids.add(item["id"])
    for innovation in plan_payload["innovations"]:
        for evidence in innovation["paper_evidence"]:
            # quote_match：full=逐字在那一页；partial=只有开头 60 字符匹配（容忍轻微抄错，
            # 但必须如实标注，不能让"真开头+编造后半段"看起来和逐字引用一样）。
            match = paper.quote_match(evidence["page"], evidence["quote"]) if paper else None
            evidence["quote_match"] = match
            evidence["verified"] = match in {"full", "partial"} if paper else None

    ctx.state["plan"] = plan_payload

    summary = {
        "innovations": len(plan_payload["innovations"]),
        "unverified_quotes": len(unverified),
        "quote_rejections": rejections,
    }
    await ctx.bus.emit(
        "plan_ready",
        plan=plan_payload,
        unverified_quotes=len(unverified),
        warning=(
            "有引文未通过核验，已标记 verified=false，前端会显示为'未核验'。"
            if unverified
            else ""
        ),
    )
    return ToolResult(
        content=json_content(
            {
                "accepted": True,
                **summary,
                "note": "清单已提交，阶段 A 结束。",
            }
        ),
        details=summary,
        terminate=True,
    )


RECORD_PLAN = Tool(
    name="record_plan",
    description=(
        "提交你梳理出的核心创新点清单，**阶段 A 必须以此结束**。"
        "每条创新点都要附论文证据（页码 + 原文片段），后端会逐条核验引文是否真的在那一页。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "paper_summary": {"type": "string", "description": "两三句话概括这篇论文做了什么"},
            "coverage_note": {"type": "string", "description": "你读了哪些页、还有什么没看"},
            "innovations": {
                "type": "array",
                "minItems": 1,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "短标识，例如 inn-1"},
                        "name": {"type": "string", "description": "创新点名称，例如 低秩重参数化"},
                        "one_liner": {"type": "string", "description": "一句话解释它是什么"},
                        "difficulty": {"type": "string", "enum": ["beginner", "medium", "hard"]},
                        "paper_evidence": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "page": {"type": "integer", "minimum": 1},
                                    "quote": {"type": "string", "minLength": 8},
                                    "kind": {
                                        "type": "string",
                                        "enum": ["text", "equation", "figure", "table"],
                                    },
                                },
                                "required": ["page", "quote"],
                            },
                        },
                        "search_hints": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "阶段 B 用来在代码里搜索的线索：可能的类名/函数名/变量名/超参数名"
                                "（英文，越像真实标识符越好）"
                            ),
                        },
                    },
                    "required": ["id", "name", "one_liner", "paper_evidence"],
                },
            },
        },
        "required": ["paper_summary", "innovations"],
        "additionalProperties": False,
    },
    handler=_record_plan,
)

# ===========================================================================
# 阶段 B 的提交工具：record_finding
# ===========================================================================
class CodeEvidence(BaseModel):
    """一条代码引用。

    注意 `snippet_sha256` **不由模型提供**：那是后端把这段行读出来自己算的。
    模型说"第 42-55 行实现了这个公式"是**主张**，后端去把那段行读出来算哈希才是**证据**。
    """

    path: str = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    symbol: str | None = None
    why: str = Field(min_length=1, description="这段代码为什么对应那个创新点")
    quote: str | None = Field(default=None, description="（可选）这段行里的原文片段，会被核验")


class Explanation(BaseModel):
    """解释。字段不多，但每个都有硬性要求——"一句话带过"不算解释。"""

    intuition: str = Field(default="", description="用生活化的话讲清它在干什么（至少 40 字）")
    math: str = Field(default="", description="数学表达（LaTeX，前端会标注为'模型重构'）")
    code_walkthrough: list[dict[str, str]] = Field(
        default_factory=list, description="逐段讲解引用的代码，line_ref 必须指向真实行号"
    )
    pitfalls: list[str] = Field(default_factory=list, description="容易搞错的地方（至少 1 条）")
    read_next: list[str] = Field(default_factory=list, description="接下来该看哪里")


class Finding(BaseModel):
    innovation_id: str = Field(min_length=1)
    status: Literal["matched", "partial", "not_found"]
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_reason: str = Field(min_length=1)
    code_evidence: list[CodeEvidence] = Field(default_factory=list)
    explanation: Explanation = Field(default_factory=Explanation)
    not_found_reason: str | None = None
    searched: list[str] = Field(default_factory=list)


async def _record_finding(args: dict, ctx: ToolContext) -> ToolResult:
    from ...repo_source import RepoSource
    from ...verify import check_evidence

    repo: RepoSource | None = ctx.repo
    if repo is None:
        raise ToolError("本次运行没有关联仓库，无法提交代码引用")

    targets: dict[str, dict] = ctx.state.get("targets") or {}
    if not targets:
        raise ToolError("没有待定位的创新点（selected 为空）")

    quote_rejections = int(ctx.state.get("finding_quote_rejections", 0))
    quality_rejections = int(ctx.state.get("finding_quality_rejections", 0))

    try:
        finding = Finding.model_validate(args)
    except ValidationError as exc:
        lines = [
            f"- {'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:6]
        ]
        raise ToolError("finding 结构不合格，请修正后重新提交：\n" + "\n".join(lines)) from exc

    if finding.innovation_id not in targets:
        raise ToolError(
            f"innovation_id={finding.innovation_id} 不在本次要定位的清单里。"
            f"本次只处理：{sorted(targets)}。"
            "不要重复提交清单外的 id（会被一直拒绝）；如果你手上的清单已经全部提交完，"
            "直接调用 finish 结束即可。"
        )

    # --- 结构性要求：找到要有引用，找不到要有理由和搜索记录 ---
    if finding.status == "not_found":
        if not (finding.not_found_reason or "").strip():
            raise ToolError("status=not_found 时必须填 not_found_reason（为什么判断它没有实现）")
        if not finding.searched:
            raise ToolError(
                "status=not_found 时必须填 searched（你搜过哪些关键词/文件）——"
                "否则无法区分'真的没有'和'你没去找'"
            )
    elif not finding.code_evidence:
        raise ToolError(f"status={finding.status} 时必须至少给一条 code_evidence")

    # --- 逐条核验：主张 → 证据 ---
    failures: list[str] = []
    evidence_payload: list[dict] = []
    for index, evidence in enumerate(finding.code_evidence):
        raw = evidence.model_dump()
        outcome = check_evidence(repo, raw)
        if outcome["state"] != "verified":
            for failure in outcome["failures"]:
                failures.append(
                    f"code_evidence[{index}] {evidence.path}:{evidence.line_start}-{evidence.line_end} → {failure}"
                )
        raw["snippet_sha256"] = outcome["snippet_sha256"]
        raw["verification"] = {
            "state": outcome["state"],
            "checked_at": None,  # 交付前 verify_artifact() 会正式填一次
            "failures": outcome["failures"],
            "file_lines": outcome["line_count"],
        }
        evidence_payload.append(raw)

    quality_problems = _explanation_problems(finding)

    blocked_quote = bool(failures) and quote_rejections < MAX_QUOTE_REJECTIONS
    blocked_quality = bool(quality_problems) and quality_rejections < MAX_QUALITY_REJECTIONS
    if blocked_quote or blocked_quality:
        # 一次把问题说全：引用的问题和解释的问题一起回给模型，省一个来回
        sections: list[str] = []
        if blocked_quote:
            ctx.state["finding_quote_rejections"] = quote_rejections + 1
            sections.append(
                "代码引用没通过核验：\n"
                + "\n".join(f"- {item}" for item in failures)
                + "\n（请用 read_file 重新确认文件路径与行号，或改用真正的实现位置）"
            )
        if blocked_quality:
            ctx.state["finding_quality_rejections"] = quality_rejections + 1
            sections.append("解释的信息量不够：\n" + "\n".join(f"- {item}" for item in quality_problems))
        raise ToolError(
            "这条结论还不能交付：\n\n" + "\n\n".join(sections) + "\n\n请修正后重新调用 record_finding。"
        )

    target = targets[finding.innovation_id]
    payload = {
        "id": finding.innovation_id,
        # 论文侧信息从清单里拷过来，让产物自洽（verify 可以只靠产物重放）
        "name": target.get("name"),
        "one_liner": target.get("one_liner"),
        "difficulty": target.get("difficulty"),
        "paper_evidence": target.get("paper_evidence", []),
        "search_hints": target.get("search_hints", []),
        # 代码侧
        "status": finding.status,
        "confidence": finding.confidence,
        "confidence_reason": finding.confidence_reason,
        "code_evidence": evidence_payload,
        "explanation": finding.explanation.model_dump(),
        "not_found_reason": finding.not_found_reason,
        "searched": finding.searched,
    }

    findings: list[dict] = ctx.state.setdefault("findings", [])
    replaced = False
    for position, existing in enumerate(findings):
        if existing["id"] == finding.innovation_id:
            findings[position] = payload
            replaced = True
            break
    if not replaced:
        findings.append(payload)

    await ctx.bus.emit(
        "finding",
        finding=payload,
        innovation_id=finding.innovation_id,
        status=finding.status,
        citations=len(evidence_payload),
        unverified=len(failures),
    )
    return ToolResult(
        content=json_content(
            {
                "accepted": True,
                "innovation_id": finding.innovation_id,
                "status": finding.status,
                "citations": len(evidence_payload),
                "unverified_citations": len(failures),
                "explanation_problems": len(quality_problems),
                "replaced_previous": replaced,
                "progress": f"{len(findings)}/{len(targets)} 条创新点已给出结论",
                "note": (
                    "有引用未通过核验，已标记，前端会显示为未核验。"
                    if failures
                    else "全部引用通过核验。"
                ),
            }
        ),
        details={"innovation_id": finding.innovation_id, "status": finding.status},
    )


RECORD_FINDING = Tool(
    name="record_finding",
    description=(
        "为**一条**创新点提交结论，阶段 B 每条创新点调用一次。"
        "找到了就给 code_evidence（path + 行号区间 + 为什么对应），后端会把那段代码读出来核验；"
        "确实没有实现就如实报 not_found，并写清你搜过什么。找不到不是失败，编造才是。\n"
        "**解释要能建立起对应关系**：intuition 至少 40 字、讲清它在做什么；matched/partial 必须给 "
        "code_walkthrough（逐段讲解，line_ref 指向真实行号）；任何状态都要给 pitfalls（容易搞错的地方）。"
        "写得太浅会被打回重写。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "innovation_id": {"type": "string", "description": "要提交的创新点 id（必须来自给定清单）"},
            "status": {
                "type": "string",
                "enum": ["matched", "partial", "not_found"],
                "description": "matched=找到完整实现；partial=只找到部分/近似；not_found=仓库里没有",
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "你对这个结论的置信度",
            },
            "confidence_reason": {
                "type": "string",
                "description": "为什么是这个置信度（写清不确定的地方）",
            },
            "code_evidence": {
                "type": "array",
                "description": "代码引用。status 不是 not_found 时至少一条。",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "相对仓库根目录的路径"},
                        "line_start": {"type": "integer", "minimum": 1},
                        "line_end": {"type": "integer", "minimum": 1},
                        "symbol": {"type": "string", "description": "函数/类名（可选）"},
                        "why": {"type": "string", "description": "这段代码为什么对应那个创新点"},
                        "quote": {"type": "string", "description": "（可选）这段行里的原文片段，会被核验"},
                    },
                    "required": ["path", "line_start", "line_end", "why"],
                },
            },
            "explanation": {
                "type": "object",
                "properties": {
                    "intuition": {"type": "string", "description": "它在做什么：先讲动机/类比，再上术语"},
                    "math": {"type": "string", "description": "数学说明（LaTeX，前端会标注为'模型重构'）"},
                    "pitfalls": {"type": "array", "items": {"type": "string"}, "description": "容易搞错的地方（至少 1 条）"},
                    "code_walkthrough": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "line_ref": {"type": "string"},
                                "text": {"type": "string"},
                            },
                            "required": ["line_ref", "text"],
                        },
                    },
                    "read_next": {"type": "array", "items": {"type": "string"}},
                },
            },
            "not_found_reason": {"type": "string", "description": "status=not_found 时必填"},
            "searched": {
                "type": "array",
                "items": {"type": "string"},
                "description": "status=not_found 时必填：你搜过哪些关键词/文件",
            },
        },
        "required": ["innovation_id", "status", "confidence", "confidence_reason"],
        "additionalProperties": False,
    },
    handler=_record_finding,
)

__all__ = ["RECORD_PLAN", "RECORD_FINDING", "Plan", "Innovation", "Finding"]
