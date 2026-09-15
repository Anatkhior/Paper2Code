"""仓库侧工具（阶段 B / locate 用）。

和论文侧一样保持"原子"：列目录、搜关键词、读文件。
**没有**"找到某个算法的实现"这种工具——那是决策，决策属于 Agent。

所有工具的返回都带"是否被截断/是否搜完"的明确标注：
因为"没找到"和"没搜完"是两件完全不同的事，Agent 必须能区分。
"""

from __future__ import annotations

from ...repo_source import RepoError, RepoSource
from .base import Tool, ToolContext, ToolError, ToolResult, json_content, truncate

MAX_READ_CHARS = 12_000   # ≈3000 token
MAX_SEARCH_HITS = 40


def _repo(ctx: ToolContext) -> RepoSource:
    if ctx.repo is None:
        raise ToolError("本次运行没有关联仓库（这是阶段 A 或纯链路测试）")
    return ctx.repo


async def _repo_tree(args: dict, ctx: ToolContext) -> ToolResult:
    repo = _repo(ctx)
    subdir = str(args.get("path") or "")
    depth = int(args.get("depth") or 2)
    try:
        payload = repo.tree(subdir, depth=max(1, min(depth, 5)))
    except RepoError as exc:
        raise ToolError(str(exc)) from exc
    payload["commit_sha"] = repo.commit_sha
    payload["hint"] = "size 是字节数。判断某个文件里到底写了什么，请用 read_file，不要靠文件名猜。"
    return ToolResult(content=json_content(payload), details={"rows": len(payload["rows"])})


async def _search_code(args: dict, ctx: ToolContext) -> ToolResult:
    repo = _repo(ctx)
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        raise ToolError("pattern 必须是非空字符串")
    glob = str(args.get("glob") or "**/*")
    try:
        payload = repo.search(pattern, glob=glob, max_hits=MAX_SEARCH_HITS)
    except RepoError as exc:
        raise ToolError(str(exc)) from exc
    return ToolResult(
        content=json_content(payload),
        details={"hits": len(payload["hits"]), "complete": payload["complete"]},
    )


async def _read_file(args: dict, ctx: ToolContext) -> ToolResult:
    repo = _repo(ctx)
    path = args.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ToolError("path 必须是非空字符串")
    start = args.get("start_line")
    end = args.get("end_line")
    for name, value in (("start_line", start), ("end_line", end)):
        if value is not None and not isinstance(value, int):
            raise ToolError(f"{name} 必须是整数")
    try:
        payload = repo.read_file(path, start, end)
    except RepoError as exc:
        raise ToolError(str(exc)) from exc

    numbered, truncated = truncate(payload["numbered"], MAX_READ_CHARS)
    header = f"{payload['path']} 第 {payload['line_start']}–{payload['line_end']} 行（全文共 {payload['line_count_total']} 行）"
    return ToolResult(
        content=f"{header}\n{numbered}",
        details={
            "path": payload["path"],
            "line_start": payload["line_start"],
            "line_end": payload["line_end"],
            "truncated": truncated,
        },
    )


REPO_TOOLS: list[Tool] = [
    Tool(
        name="repo_tree",
        description=(
            "列出仓库的目录结构（默认只看两层）。用来判断这是个什么项目、代码放在哪。"
            "会自动跳过 .git / node_modules / 依赖目录等噪音。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "子目录，留空表示仓库根目录"},
                "depth": {"type": "integer", "description": "向下看几层，默认 2"},
            },
            "additionalProperties": False,
        },
        handler=_repo_tree,
    ),
    Tool(
        name="search_code",
        description=(
            "在仓库里做正则搜索（大小写不敏感），返回 文件:行:内容。"
            "这是你在几十万行代码里定位实现的主要手段：先用论文里的术语/超参数名/可能的类名试，"
            "再根据命中结果收敛。返回里会说明搜索是否被截断——没搜完时'没找到'不作数。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式，例如 'class .*LoRA' 或 'lora_alpha'"},
                "glob": {"type": "string", "description": "限定文件，例如 '**/*.py'（默认全部）"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        handler=_search_code,
    ),
    Tool(
        name="read_file",
        description=(
            "读取文件内容，带行号。**强烈建议传 start_line/end_line 只读你要的那一段**——"
            "整文件读进来会挤爆上下文。返回的行号可以直接用在 record_finding 里。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对仓库根目录的路径"},
                "start_line": {"type": "integer", "description": "起始行（从 1 开始）"},
                "end_line": {"type": "integer", "description": "结束行（含）"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=_read_file,
    ),
]

# 工具集的名字单独导出：mock provider 用它判断当前处于哪个阶段的脚本
REPO_TOOL_NAMES = {tool.name for tool in REPO_TOOLS}

__all__ = ["REPO_TOOLS", "REPO_TOOL_NAMES"]
