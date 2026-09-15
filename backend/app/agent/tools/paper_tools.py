"""论文侧工具（阶段 A / recon 用）。

这几个工具刻意保持"原子"：列页、读页、搜关键词、读全文。
**没有**"找 Method 章节"这种工具——那是一个决策，决策属于 Agent。

每个工具的返回都有上限并且会明确标注 [已截断]（docs/v0-spec.md §5）。
"""

from __future__ import annotations

from ...paper import PaperDocument
from .base import Tool, ToolContext, ToolError, ToolResult, json_content, truncate

# 上限都在这里，方便一处调整
MAX_PAGE_CHARS = 16_000        # ≈4000 token
MAX_FULLTEXT_CHARS = 160_000   # ≈40k token
MAX_SEARCH_HITS = 10
MAX_TREE_ROWS = 400


def _paper(ctx: ToolContext) -> PaperDocument:
    if ctx.paper is None:
        raise ToolError("本次运行没有关联论文（这是阶段 B 或纯链路测试）")
    return ctx.paper


async def _list_pages(args: dict, ctx: ToolContext) -> ToolResult:
    paper = _paper(ctx)
    stats = paper.page_stats()
    rows = [
        {"page": s.page, "chars": s.chars, "first_line": s.first_line}
        for s in stats[:MAX_TREE_ROWS]
    ]
    payload = {
        "page_count": paper.page_count,
        "title_guess": paper.title_guess,
        "pages": rows,
        "hint": (
            "chars 是这一页的字符数。不要只靠 first_line 猜内容——"
            "需要判断某页讲什么就直接 get_page_text 读它。"
        ),
    }
    return ToolResult(
        content=json_content(payload),
        details={"page_count": paper.page_count, "returned": len(rows)},
    )


async def _get_page_text(args: dict, ctx: ToolContext) -> ToolResult:
    paper = _paper(ctx)
    page = args.get("page")
    if not isinstance(page, int):
        raise ToolError("page 必须是整数（从 1 开始）")
    try:
        text = paper.page_text(page)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    text, truncated = truncate(text, MAX_PAGE_CHARS, note="；需要后半部分请用 search_paper 定位")
    return ToolResult(
        content=text,
        details={"page": page, "truncated": truncated, "chars": len(text)},
    )


async def _search_paper(args: dict, ctx: ToolContext) -> ToolResult:
    paper = _paper(ctx)
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("query 必须是非空字符串")
    hits = paper.search(query, max_hits=MAX_SEARCH_HITS)
    if not hits:
        return ToolResult(
            content=json_content(
                {
                    "query": query,
                    "hits": [],
                    "hint": "没有命中。换个同义词/缩写再试，或者直接 get_page_text 读相关页。",
                }
            ),
            details={"hits": 0},
        )
    return ToolResult(content=json_content({"query": query, "hits": hits}), details={"hits": len(hits)})


async def _read_paper_all(args: dict, ctx: ToolContext) -> ToolResult:
    paper = _paper(ctx)
    text = paper.full_text()
    if len(text) > MAX_FULLTEXT_CHARS:
        raise ToolError(
            f"全文共 {len(text)} 字符，超过单次上限 {MAX_FULLTEXT_CHARS}。"
            "请改用 list_pages 看结构，再用 get_page_text 读你真正需要的页。"
        )
    return ToolResult(
        content=text,
        details={"pages": paper.page_count, "chars": len(text)},
    )


PAPER_TOOLS: list[Tool] = [
    Tool(
        name="list_pages",
        description=(
            "列出论文每一页的页号和字符数（外加该页首行）。"
            "用来判断论文结构、决定接下来读哪几页。它不返回正文。"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_list_pages,
    ),
    Tool(
        name="get_page_text",
        description="读取指定页的完整文本。页码从 1 开始。",
        parameters={
            "type": "object",
            "properties": {"page": {"type": "integer", "description": "页码，从 1 开始"}},
            "required": ["page"],
            "additionalProperties": False,
        },
        handler=_get_page_text,
    ),
    Tool(
        name="search_paper",
        description=(
            "在论文里做关键词搜索（大小写不敏感的子串匹配，不是语义检索），"
            "返回命中页码和上下文片段。适合找某个术语、公式名、章节标题出现在哪里。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要搜索的关键词，例如 'low-rank'"}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_search_paper,
    ),
    Tool(
        name="read_paper_all",
        description=(
            "一次性读取全文（带页码分隔标记）。"
            "注意成本：每次工具调用都会把之前的全部历史重新发给模型，"
            "所以对短论文（≤15 页）一次读完通常比逐页翻更省；长论文请改用 list_pages + get_page_text。"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_read_paper_all,
    ),
]

__all__ = ["PAPER_TOOLS"]
