"""工具基类与安全护栏。

工具的铁律（docs/v0-spec.md §5）：
- 每个工具都必须能在返回超限时截断，并明确标注 [已截断]；
- 所有路径参数必须经过 safe_path() 白名单校验。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ...events import RunBus


@dataclass(slots=True)
class Budget:
    """§8 预算护栏。超限不是崩溃，而是"用已确认的部分交付"。"""

    max_tool_calls: int
    max_input_tokens: int
    wall_clock_seconds: int
    started: float
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def exceeded(self, now: float) -> str | None:
        if self.tool_calls >= self.max_tool_calls:
            return f"工具调用次数达到上限 {self.max_tool_calls}"
        if self.input_tokens >= self.max_input_tokens:
            return f"输入 token 达到上限 {self.max_input_tokens}"
        if now - self.started >= self.wall_clock_seconds:
            return f"运行时长达到上限 {self.wall_clock_seconds}s"
        return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "tool_calls": self.tool_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "seconds": round(self.elapsed, 1),
            "limits": {
                "max_tool_calls": self.max_tool_calls,
                "max_input_tokens": self.max_input_tokens,
                "wall_clock_seconds": self.wall_clock_seconds,
            },
        }

    @property
    def elapsed(self) -> float:
        import time

        return time.monotonic() - self.started


@dataclass(slots=True)
class ToolContext:
    """工具的运行时上下文。

    paper  —— 阶段 A 的论文读取入口（阶段 B / 纯链路测试时为 None）
    repo   —— 阶段 B 的仓库读取入口（阶段 A / 纯链路测试时为 None）
    state  —— 本次 run 内的可变状态。工具用它做"给它一次改正机会，但别无限循环"这类控制。
    """

    run_id: str
    run_dir: Path
    bus: RunBus
    paper: Any = None
    repo: Any = None
    state: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolResult:
    content: str
    details: dict[str, Any] = field(default_factory=dict)
    terminate: bool = False


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolError(Exception):
    """工具内部的可预期失败。会被翻译成一条 is_error 的 tool 结果回给模型。"""


def truncate(text: str, max_chars: int, *, note: str = "") -> tuple[str, bool]:
    """统一截断策略：超限就截断并留下明确标记，绝不静默丢内容。"""
    if len(text) <= max_chars:
        return text, False
    marker = f"\n\n[已截断：原文 {len(text)} 字符，仅显示前 {max_chars} 字符]{note}"
    return text[:max_chars] + marker, True


def safe_path(base: Path, candidate: str) -> Path:
    """路径白名单：解析后必须落在 base 之内。

    防的是 ../../etc/passwd 这类路径穿越（§10）。
    """
    if not candidate or candidate.strip() in {"", "."}:
        raise ToolError("路径不能为空")
    base_real = base.resolve()
    target = (base_real / candidate).resolve()
    if target != base_real and base_real not in target.parents:
        raise ToolError(f"路径越界：{candidate} 不在允许的目录内")
    return target


def json_content(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)
