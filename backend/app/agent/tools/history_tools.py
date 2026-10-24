"""Read-only access to older conversation turns; no credentials or write tools."""
from ...chat import load_transcript
from .base import Tool, ToolContext, ToolError, ToolResult, json_content, truncate


async def read_chat_history(args: dict, ctx: ToolContext) -> ToolResult:
    offset, limit = args.get("offset", 0), args.get("limit", 8)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise ToolError("offset 必须为非负整数，limit 必须为 1–20 的整数")
    turns = load_transcript(ctx.run_id)
    rows = [{"index": index, **turn} for index, turn in enumerate(turns[offset:offset + limit], offset)]
    content, truncated = truncate(json_content({"total": len(turns), "offset": offset, "turns": rows}), 30000,
                                  note="可缩小 limit 重新读取。")
    return ToolResult(content=content, details={"offset": offset, "count": len(rows), "truncated": truncated})


READ_CHAT_HISTORY = Tool(
    name="read_chat_history",
    description="读取本次解读中更早保存的对话。offset 从 0 开始，返回原始文字及原模型来源；只读，不会修改历史。",
    parameters={"type": "object", "properties": {
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
    }, "required": ["offset", "limit"], "additionalProperties": False},
    handler=read_chat_history,
)
