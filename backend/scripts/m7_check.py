"""阶段③（v1）离线验收：追问对话。

用户要的是"能一直问下去，而且要问得动真格"——所以这一阶段的验收重点是三件事：

A. 对话事件链与落盘：问一句 → 它自己决定查不查 → 回答落盘 → 刷新页面还在。
B. **每条消息有独立的工具预算**：模型想原地打转也只能烧 8 次工具调用，不能吃掉整个 run 的额度。
C. **回答里的代码位置会被机械核对**：回答是自由文本，但写进去的 `文件:行号`
   会被逐条拿去 git 对象里查，核不过的标红——追问不能变成新的幻觉来源。
D. 历史确实传进了下一轮（用一个"能作证"的假端点行为来验证，而不是靠读代码猜）。

运行：
    cd backend && .venv/bin/python -m scripts.m7_check
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from typing import Any

import httpx

from .harness import (
    APP,
    APP_PORT,
    MOCK_PORT,
    Checker,
    collect_sse,
    current_max_event_id,
    events_of,
    provider,
    start_service,
    stop_services,
    wait_http,
    ROOT,
)

from tests.paper_fixture import ensure_fixtures
from tests.repo_fixture import build_repo

FRONTEND = ROOT.parent / "frontend"
FRONTEND_PORT = 3302


def frontend_env() -> dict[str, str]:
    workspace = ROOT.parent
    return {
        **os.environ,
        "XDG_CACHE_HOME": str(workspace / ".cache"),
        "XDG_CONFIG_HOME": str(workspace / ".config"),
        "XDG_DATA_HOME": str(workspace / ".local" / "share"),
        "npm_config_store_dir": str(workspace / ".pnpm-store"),
        "NEXT_TELEMETRY_DISABLED": "1",
    }


async def ask(
    client: httpx.AsyncClient,
    check: Checker,
    run_id: str,
    message: str,
    *,
    model: str = "mock-model",
    context_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    baseline = await current_max_event_id(client, run_id)
    response = await client.post(
        f"/api/runs/{run_id}/chat",
        json={"provider": provider(model), "message": message, "context_ids": context_ids or []},
    )
    check(response.status_code == 200, f"提问被接受（HTTP {response.status_code}）")
    events, _, _ = await collect_sse(client, f"/api/runs/{run_id}/events?from_id={baseline}", since_id=baseline)
    replies = events_of(events, "chat_reply")
    check(len(replies) == 1, f"收到 1 条 chat_reply（实际 {len(replies)}）")
    return events, (replies[0]["data"] if replies else {})


async def prepare_analyzed_run(client: httpx.AsyncClient, check: Checker) -> str:
    """跑到阶段 B 结束：这样对话才有产物、有仓库可查。"""
    ensure_fixtures(ROOT)
    pdf = (ROOT / "tests" / "fixtures" / "synthetic_paper.pdf").read_bytes()
    run_id = (
        await client.post(
            "/api/runs",
            files={"file": ("synthetic_paper.pdf", pdf, "application/pdf")},
            data={"provider": json.dumps(provider("mock-model"))},
        )
    ).json()["run_id"]

    baseline = await current_max_event_id(client, run_id)
    await client.post(f"/api/runs/{run_id}/recon", json={"provider": provider("mock-model")})
    await collect_sse(client, f"/api/runs/{run_id}/events?from_id={baseline}", since_id=baseline)

    baseline = await current_max_event_id(client, run_id)
    await client.post(
        f"/api/runs/{run_id}/locate",
        json={"provider": provider("mock-model"), "repo_url": str(build_repo(ROOT))},
    )
    await collect_sse(client, f"/api/runs/{run_id}/events?from_id={baseline}", since_id=baseline)
    return run_id


# ---------------------------------------------------------------------------
# A. 事件链 / 落盘 / 会自己去查
# ---------------------------------------------------------------------------
async def section_a(check: Checker, client: httpx.AsyncClient) -> str:
    check.section("A. 问一句：自己决定查不查、回答落盘")
    run_id = await prepare_analyzed_run(client, check)

    events, reply = await ask(client, check, run_id, "那两条窄矩阵到底是怎么加回主干的？", context_ids=["inn-1"])
    types = [event["type"] for event in events]
    check(types[0] == "chat_user", "第一条事件是用户的提问（时间线里看得到）")
    check("run_start" in types and events_of(events, "run_start")[0]["data"]["meta"]["phase"] == "chat", "标明这是 chat 阶段")
    check("assistant_text" in types, "回答是流式输出的（不是等半天一次性蹦出来）")
    check(types[-1] == "run_end", "以 run_end 收尾")

    tool_calls = [event["data"]["tool"] for event in events_of(events, "tool_call")]
    check("read_file" in tool_calls, f"它自己去查了代码（{tool_calls}）")
    check(reply.get("tools_used") == tool_calls[: len(reply.get("tools_used") or [])] or bool(reply.get("tools_used")), "回答里记录了用过哪些工具")
    check(bool(reply.get("message")), f"回答非空：{str(reply.get('message'))[:60]}…")

    citations = reply.get("citations") or []
    check(len(citations) >= 1, f"回答里提到的代码位置被抽出来核对（{len(citations)} 处）")
    check(all(item["verified"] for item in citations), f"全部通过核对：{[c['path'] + ':' + str(c['line_start']) for c in citations]}")

    transcript = (await client.get(f"/api/runs/{run_id}/chat")).json()["turns"]
    check(len(transcript) == 2, f"对话落盘（{len(transcript)} 轮：一问一答）")
    check(transcript[0]["role"] == "user" and transcript[1]["role"] == "assistant", "顺序正确")
    check(transcript[1]["text"] == reply["message"], "落盘的文本与事件里的一致")
    check(
        (ROOT / "data" / run_id / "chat.jsonl").exists(),
        "对话写进了 chat.jsonl（刷新页面能恢复）",
    )
    return run_id


# ---------------------------------------------------------------------------
# B. 历史传进下一轮
# ---------------------------------------------------------------------------
async def section_b(check: Checker, client: httpx.AsyncClient, run_id: str) -> None:
    check.section("B. 历史确实带进了下一轮")
    _, reply = await ask(client, check, run_id, "那 scaling 是学习率吗？", model="chat-direct")
    check(
        "接着上面说" in (reply.get("message") or ""),
        "假端点作证：它收到了'之前的对话'（历史真的传进去了）",
    )
    transcript = (await client.get(f"/api/runs/{run_id}/chat")).json()["turns"]
    check(len(transcript) == 4, f"对话累积到 {len(transcript)} 轮")
    check((reply.get("tools_used") or []) == [], "这个变体选择不查代码就直接答（答得出来就不必查）")


# ---------------------------------------------------------------------------
# C. 每条消息的工具预算
# ---------------------------------------------------------------------------
async def section_c(check: Checker, client: httpx.AsyncClient, run_id: str) -> None:
    check.section("C. 每条消息的工具预算（不许吃掉整个 run 的额度）")
    started = time.monotonic()
    events, reply = await ask(client, check, run_id, "把仓库里每个文件都读一遍再回答我", model="chat-chatty")
    tool_calls = events_of(events, "tool_call")
    end = events_of(events, "run_end")[0]["data"]
    check(
        len(tool_calls) <= 8,
        f"最多只烧了 {len(tool_calls)} 次工具调用（每条消息的上限是 8，run 级上限是 40）",
    )
    check(
        "工具调用次数达到上限 8" in end["stopped_reason"],
        f"停止原因说清是这条消息的预算用完了：{end['stopped_reason'][:60]}",
    )
    check(
        "预算" in (reply.get("message") or ""),
        f"即使预算耗尽，也给了用户一句话交代：{str(reply.get('message'))[:60]}",
    )
    check(time.monotonic() - started < 60, "没有拖很久（预算同时限制了时间）")


# ---------------------------------------------------------------------------
# D. 回答里的代码位置会被核对
# ---------------------------------------------------------------------------
async def section_d(check: Checker, client: httpx.AsyncClient, run_id: str) -> None:
    check.section("D. 回答里的代码位置要经得起核对")
    _, reply = await ask(client, check, run_id, "初始化那段代码在哪里？", model="chat-badcite")
    citations = reply.get("citations") or []
    bad = [item for item in citations if not item["verified"]]
    good = [item for item in citations if item["verified"]]
    check(len(good) >= 1, f"正确的位置核对通过（{len(good)} 处）")
    check(len(bad) >= 1, f"编造的位置被标出来（{len(bad)} 处）")
    if bad:
        check(
            bad[0]["path"].endswith("does_not_exist.py") or "不存在" in bad[0].get("reason", ""),
            f"并给出了原因：{bad[0].get('reason', '')[:70]}",
        )
    check(reply.get("unverified") == len(bad), "事件里带了未通过核对的数量，前端据此标红")


# ---------------------------------------------------------------------------
# E. 没有仓库时也能聊
# ---------------------------------------------------------------------------
async def section_e(check: Checker, client: httpx.AsyncClient) -> None:
    check.section("E. 只跑过侦察（没有仓库）时也能追问")
    pdf = (ROOT / "tests" / "fixtures" / "synthetic_paper.pdf").read_bytes()
    run_id = (
        await client.post(
            "/api/runs",
            files={"file": ("synthetic_paper.pdf", pdf, "application/pdf")},
            data={"provider": json.dumps(provider("mock-model"))},
        )
    ).json()["run_id"]
    baseline = await current_max_event_id(client, run_id)
    await client.post(f"/api/runs/{run_id}/recon", json={"provider": provider("mock-model")})
    await collect_sse(client, f"/api/runs/{run_id}/events?from_id={baseline}", since_id=baseline)

    _, reply = await ask(client, check, run_id, "这篇论文的核心思路是什么？")
    check(bool(reply.get("message")), "没有克隆仓库时照样能回答")
    check((reply.get("citations") or []) == [], "没有仓库就不会凭空给出代码位置")


# ---------------------------------------------------------------------------
# F. 界面
# ---------------------------------------------------------------------------
async def section_f(check: Checker) -> None:
    check.section("F. 界面：追问入口")
    if os.environ.get("PAPERLENS_SKIP_FRONTEND"):
        print("   （PAPERLENS_SKIP_FRONTEND 已设置，跳过）", flush=True)
        return
    if not (FRONTEND / "node_modules").exists() or not shutil.which("pnpm"):
        check(False, "前端依赖或 pnpm 不可用")
        return

    build = subprocess.run(
        ["pnpm", "build"], cwd=FRONTEND, env=frontend_env(), capture_output=True, text=True, timeout=600
    )
    check(build.returncode == 0, "pnpm build 通过")
    if build.returncode != 0:
        print((build.stdout + build.stderr)[-1200:], flush=True)
        return

    server = subprocess.Popen(
        [str(FRONTEND / "node_modules" / ".bin" / "next"), "start", "-p", str(FRONTEND_PORT)],
        cwd=FRONTEND,
        env=frontend_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await wait_http(f"http://127.0.0.1:{FRONTEND_PORT}/", timeout=60)
        async with httpx.AsyncClient() as plain:
            html = (await plain.get(f"http://127.0.0.1:{FRONTEND_PORT}/", timeout=30)).text
        for marker, description in {
            "8. 追问": "追问面板",
            "它可以自己去翻论文和代码": "能力说明（每条消息的预算）",
            "还没问过": "空状态引导",
            "就这条追问": "从结论卡片发起追问",
        }.items():
            check(marker in html, f"页面里有「{description}」（{marker}）")
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()


async def main() -> int:
    check = Checker("阶段③（追问对话）验收")
    mock = start_service("devtools.mock_provider:app", MOCK_PORT)
    app_proc = start_service(
        "app.main:app", APP_PORT, extra_env={"PAPERLENS_ALLOW_LOCAL_REPO_PATHS": "true"}
    )
    try:
        await wait_http(f"http://127.0.0.1:{MOCK_PORT}/health")
        await wait_http(f"{APP}/api/health")
        print(f"mock provider : {MOCK_PORT}\nPaperLens API : {APP}", flush=True)

        async with httpx.AsyncClient(base_url=APP, timeout=300) as client:
            run_id = await section_a(check, client)
            await section_b(check, client, run_id)
            await section_c(check, client, run_id)
            await section_d(check, client, run_id)
            await section_e(check, client)

        await section_f(check)
        return check.finish()
    finally:
        stop_services(mock, app_proc)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
