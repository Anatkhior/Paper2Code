"""Agent 工具循环（v0 单 Agent）。

这是 docs/v0-spec.md §5「Agent 自主探索」的落点：
读什么、读多少、什么时候停，全部由模型决定；
后端只做三件事——跑循环、记账、把过程推给前端。

M1 之后会再加"先列清单 → 用户勾选 → 再定位"的两阶段流程，
但循环本身不变，所以这个文件应该长期保持很小（目标 ~200 行）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from ..events import RunBus
from ..providers import ProviderConfig, estimate_tokens, stream_turn
from .tools.base import Budget, Tool, ToolContext, ToolError, ToolResult

DEFAULT_MAX_TURNS = 30

# 连续多少轮"完全一样的工具错误"就提前停。
# 真实模型也会卡在同一个错误上反复重试，一路烧到工具调用上限；
# 与其白花钱，不如早停并把原因告诉用户。
MAX_REPEATED_ERRORS = 3


async def run_agent(
    *,
    cfg: ProviderConfig,
    tools: list[Tool],
    system_prompt: str,
    user_prompt: str,
    bus: RunBus,
    budget: Budget,
    run_dir_path,
    paper: Any = None,
    repo: Any = None,
    meta: dict[str, Any] | None = None,
    initial_state: dict[str, Any] | None = None,
    finalize: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> dict[str, Any]:
    tool_map = {tool.name: tool for tool in tools}
    tool_schemas = [tool.schema() for tool in tools] or None
    ctx = ToolContext(
        run_id=bus.run_id,
        run_dir=run_dir_path,
        bus=bus,
        paper=paper,
        repo=repo,
        state=dict(initial_state or {}),
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    await bus.emit(
        "run_start",
        provider=cfg.safe_label(),
        model=cfg.model,
        task=user_prompt[:200],
        meta=meta or {},
        limits=budget.snapshot()["limits"],
    )

    stopped_reason = "agent_finished"
    status = "ok"
    turn = 0
    coverage_note = ""
    last_error_signature: str | None = None
    repeated_errors = 0
    tools_used: list[str] = []
    last_text = ""

    async def push_text(delta: str) -> None:
        await bus.emit("assistant_text", delta=delta)

    paced_waits: list[float] = []
    budget_warned_pacing = False

    async def _warn_if_budget_infeasible() -> None:
        """按当前节奏跑不完预算就提前说，别让用户在"预算用尽"上再撞一次墙。

        限流修好之后，慢节奏会把用户推到另一个墙：端点 3 次/分钟 × 40 次调用 ≈ 13 分钟，
        超过默认 600s 墙钟。这个提示就是那堵墙的预警（不改预算本身——那是用户有意设的护栏）。
        """
        nonlocal budget_warned_pacing
        if budget_warned_pacing:
            return
        from .. import providers

        interval = providers._effective_llm_interval()
        if interval <= 0:
            return
        remaining_calls = max(0, budget.max_tool_calls - budget.tool_calls)
        expected = remaining_calls * interval
        remaining_wall = budget.wall_clock_seconds - budget.elapsed
        if expected > remaining_wall:
            budget_warned_pacing = True
            await bus.emit(
                "budget_warning",
                reason=(
                    f"按当前端点节奏（每 {interval:.1f}s 一次调用），剩余 {remaining_calls} 次调用约需 "
                    f"{expected / 60:.1f} 分钟，而墙钟预算只剩 {remaining_wall / 60:.1f} 分钟 —— "
                    "这轮很可能跑不完。要么调大 PAPERLENS_WALL_CLOCK_SECONDS，要么换限额更宽的端点。"
                ),
                used=budget.snapshot(),
            )

    try:
        while True:
            reason = budget.exceeded(time.monotonic())
            if reason and paced_waits:
                reason += f"（其中 {sum(paced_waits):.0f}s 花在端点限额等待上）"
            if reason:
                stopped_reason = reason
                await bus.emit("budget_warning", reason=reason, used=budget.snapshot())
                break
            if turn >= max_turns:
                stopped_reason = f"达到最大轮数 {max_turns}"
                await bus.emit("budget_warning", reason=stopped_reason, used=budget.snapshot())
                break

            turn += 1
            await bus.emit("step_start", turn=turn)

            budget.input_tokens += estimate_tokens(messages, tool_schemas)

            async def _on_llm_retry(
                attempt: int, delay: float, detail: str, reason: str = ""
            ) -> None:
                """端点限额相关的一切等待都要看得见：用户在时间线上看到「在等，不是卡死」。"""
                paced_waits.append(delay)
                await bus.emit(
                    "llm_retry",
                    attempt=attempt,
                    delay_seconds=round(delay, 1),
                    reason=reason or "rate_limit",
                    detail=detail[:200],
                )
                await _warn_if_budget_infeasible()

            result = await stream_turn(
                cfg, messages, tool_schemas, on_text=push_text, on_retry=_on_llm_retry
            )
            last_text = result.text
            budget.output_tokens += estimate_tokens([{"role": "assistant", "content": result.text}])

            if result.usage:
                # 端点自己报了用量就以它为准（§8 的诚实记账）
                prompt_tokens = result.usage.get("prompt_tokens")
                if isinstance(prompt_tokens, int) and prompt_tokens > 0:
                    budget.input_tokens += prompt_tokens - estimate_tokens(messages, tool_schemas)

            messages.append(result.assistant_message())

            if not result.tool_calls:
                stopped_reason = "模型没有继续调用工具"
                break

            terminate = False
            batch_error_signatures: list[str] = []
            for call in result.tool_calls:
                budget.tool_calls += 1
                tools_used.append(call.name)
                args = call.arguments()
                await bus.emit(
                    "tool_call",
                    tool=call.name,
                    args=args,
                    args_raw=call.arguments_raw,
                    call_id=call.id,
                )

                started = time.monotonic()
                is_error = False
                tool = tool_map.get(call.name)
                if tool is None:
                    is_error = True
                    content = f"[错误] 不存在名为 {call.name} 的工具。可用工具：{sorted(tool_map)}"
                    details: dict[str, Any] = {}
                elif not args and call.arguments_raw.strip() not in {"", "{}"}:
                    is_error = True
                    content = f"[错误] 参数不是合法 JSON：{call.arguments_raw[:200]}"
                    details = {}
                else:
                    try:
                        outcome = await tool.handler(args, ctx)
                        # 这个检查是有意为之：写工具时最容易犯的错就是忘了 async，
                        # 那样 handler 会返回一个协程对象而不是 ToolResult，静默出错。
                        if not isinstance(outcome, ToolResult):
                            raise TypeError(
                                f"工具 {call.name} 返回了 {type(outcome).__name__}，期望 ToolResult"
                                "（是不是忘了 async def？）"
                            )
                        content = outcome.content
                        details = outcome.details
                        terminate = terminate or outcome.terminate
                    except ToolError as exc:
                        is_error = True
                        content = f"[错误] {exc}"
                        details = {}
                    except Exception as exc:  # noqa: BLE001 —— 工具炸了要让模型知道，而不是把整个 run 弄崩
                        is_error = True
                        content = f"[错误] {type(exc).__name__}: {exc}"
                        details = {}

                if call.name == "finish" and not is_error:
                    coverage_note = str(details.get("coverage_note", ""))

                await bus.emit(
                    "tool_result",
                    tool=call.name,
                    call_id=call.id,
                    is_error=is_error,
                    ms=int((time.monotonic() - started) * 1000),
                    summary=_summarize(content),
                    details=details,
                )

                if is_error:
                    # 签名只看"哪个工具 + 错误内容开头"，用来识别"原地打转"
                    batch_error_signatures.append(f"{call.name}::{content[:120]}")

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": content,
                    }
                )

            # 整批都失败、而且和上一批的失败一模一样 → 判定为原地打转
            if batch_error_signatures and len(batch_error_signatures) == len(result.tool_calls):
                signature = "|".join(sorted(batch_error_signatures))
                if signature == last_error_signature:
                    repeated_errors += 1
                else:
                    last_error_signature = signature
                    repeated_errors = 1
                if repeated_errors >= MAX_REPEATED_ERRORS:
                    stopped_reason = (
                        f"连续 {repeated_errors} 轮遇到同一个工具错误，已提前停止（避免空转烧钱）："
                        f"{batch_error_signatures[0].split('::', 1)[-1][:160]}"
                    )
                    await bus.emit("budget_warning", reason=stopped_reason, used=budget.snapshot())
                    break
            else:
                last_error_signature = None
                repeated_errors = 0

            if terminate:
                stopped_reason = "Agent 主动结束"
                break

    except asyncio.CancelledError:
        status = "cancelled"
        stopped_reason = "用户取消"
        await bus.emit("run_end", status=status, stopped_reason=stopped_reason, usage=budget.snapshot())
        raise
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        stopped_reason = _failure_reason(exc)
        await bus.emit("error", kind=type(exc).__name__, message=str(exc)[:500])
        # **失败也要交付**：把已经记录下来的结论核验、落盘、发出 verification_done，
        # 全部赶在 run_end 之前。理由和"预算超限不是崩溃、而是用已确认的部分交付"一样：
        # 用户等了半天（可能还花了钱），不能因为第 20 轮撞上限流就把前 19 轮的成果全丢掉。
        summary = {
            "status": status,
            "stopped_reason": stopped_reason,
            "turns": turn,
            "coverage_note": coverage_note,
            "usage": budget.snapshot(),
            "final_text": last_text,
            "tools_used": tools_used,
            "plan": ctx.state.get("plan"),
            "findings": ctx.state.get("findings"),
            "partial": True,
        }
        if finalize is not None:
            try:
                summary.update(await finalize(summary))
            except Exception as finalize_exc:  # noqa: BLE001
                await bus.emit(
                    "error",
                    kind=f"finalize:{type(finalize_exc).__name__}",
                    message=str(finalize_exc)[:500],
                )
                summary.setdefault("finalize_error", str(finalize_exc)[:300])
        await bus.emit("run_end", **summary)
        return summary

    summary = {
        "status": status,
        "stopped_reason": stopped_reason,
        "turns": turn,
        "coverage_note": coverage_note,
        "usage": budget.snapshot(),
        # 这两项是给"追问对话"用的：最终回答文本 + 这次都用过哪些工具
        "final_text": last_text,
        "tools_used": tools_used,
        # 工具写进 ctx.state 的结构化产物（阶段 A 的 plan / 阶段 B 的 findings）
        "plan": ctx.state.get("plan"),
        "findings": ctx.state.get("findings"),
    }
    # finalize 必须在 run_end **之前**跑：
    # 事件契约里 run_end 是最后一条事件，前端看到它就收工关流。
    # 任何"想被用户看见的结果"都必须赶在它前面发出去。
    if finalize is not None:
        try:
            summary.update(await finalize(summary))
        except Exception as exc:  # noqa: BLE001
            await bus.emit("error", kind=f"finalize:{type(exc).__name__}", message=str(exc)[:500])
            summary.setdefault("finalize_error", str(exc)[:300])

    # 注意：这里**不关总线**。一个 run 的事件总线贯穿它的所有阶段，
    # 关了的话第二阶段的事件就发不出来了。
    await bus.emit("run_end", **summary)
    return summary


def _summarize(content: str, limit: int = 300) -> str:
    text = content.strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _failure_reason(exc: Exception) -> str:
    """失败原因要说人话（运行失败时用户唯一能看到的解释）。

    限流是最常见的一种，而且是"用户能自己解决"的那种（设限额或换端点），
    所以这里用 providers 的诊断文案，而不是干巴巴地甩一个 RateLimitError。
    """
    from .. import providers

    diagnosis = providers._diagnose_exception(exc)
    return f"{type(exc).__name__}：{diagnosis}"
