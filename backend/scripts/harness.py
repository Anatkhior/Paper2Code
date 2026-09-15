"""验收脚本共用的工具：起服务、读 SSE、断言计数。

把这段抽出来，是为了让每个里程碑的验收脚本（m0_check / m1_check / …）
都专注于"这个里程碑该验证什么"，而不是重复写 HTTP 样板。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
# 验收脚本用专属端口：这样你手动起的 mock/后端（8123/8000）不会和验收互抢，
# 也就不会出现"测试连到了旧代码的服务上"这种鬼故事。
MOCK_PORT = 8231
APP_PORT = 8232
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}/v1"
APP = f"http://127.0.0.1:{APP_PORT}"


class Checker:
    def __init__(self, title: str) -> None:
        self.title = title
        self.failures: list[str] = []

    def section(self, name: str) -> None:
        print(f"\n── {name} ──", flush=True)

    def __call__(self, condition: bool, message: str) -> None:
        print(("✅ " if condition else "❌ ") + message, flush=True)
        if not condition:
            self.failures.append(message)

    def finish(self) -> int:
        print(flush=True)
        if self.failures:
            print(f"❌ 验收未通过，{len(self.failures)} 项失败：", flush=True)
            for item in self.failures:
                print(f"   - {item}", flush=True)
            return 1
        print(f"✅ {self.title}：全部通过", flush=True)
        return 0


def provider(model: str, key: str = "mock-key") -> dict[str, Any]:
    return {
        "protocol": "openai-compatible",
        "base_url": MOCK_BASE,
        "api_key": key,
        "model": model,
    }


def assert_port_free(port: int) -> None:
    """端口必须空闲。

    这个检查是有教训的：曾经有个手动起的旧版 mock 还占着端口，
    测试进程绑不上却没人管，于是应用连到了**旧代码**的服务上，
    表现成"功能莫名其妙不生效"，查起来非常费劲。
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(
                f"端口 {port} 已被占用（{exc}）。大概率是你手动起过一个 mock/服务，先停掉再跑验收。"
            ) from exc


def start_service(module: str, port: int, extra_env: dict[str, str] | None = None) -> subprocess.Popen:
    assert_port_free(port)
    env = {**os.environ, "PYTHONPATH": str(ROOT), **(extra_env or {})}
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module, "--port", str(port), "--log-level", "warning"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def wait_http(url: str, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    async with httpx.AsyncClient() as client:
        while time.time() < deadline:
            try:
                if (await client.get(url, timeout=1)).status_code == 200:
                    return
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.25)
    raise RuntimeError(f"服务没能启动：{url}")


async def collect_sse(
    client: httpx.AsyncClient,
    path: str,
    *,
    stop_on_run_end: bool = True,
    timeout_s: float = 120.0,
    since_id: int = 0,
) -> tuple[list[dict[str, Any]], float, float]:
    """读一条 SSE 流，返回 (事件列表, 首事件时刻, 结束时刻)。

    两个坑：
    1. 必须兼容 \\r\\n —— sse-starlette 默认用 CRLF，只按 \\n\\n 切会一条都读不到。
    2. `since_id`：事件流会**重放历史**，而历史里可能有上一个阶段的 run_end。
       不过滤的话，第二个阶段刚连上就会立刻"看到 run_end"然后停止收集。
    """
    events: list[dict[str, Any]] = []
    first_at = 0.0
    async with asyncio.timeout(timeout_s):
        async with client.stream("GET", path, timeout=timeout_s) as response:
            buffer = ""
            async for chunk in response.aiter_text():
                if not first_at:
                    first_at = time.monotonic()
                buffer += chunk.replace("\r\n", "\n").replace("\r", "\n")
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    for line in block.splitlines():
                        if not line.startswith("data:"):
                            continue
                        event = json.loads(line[5:].strip())
                        if event["id"] <= since_id:
                            continue  # 历史事件：不算数，也不能用来判断"结束了"
                        events.append(event)
                if stop_on_run_end and any(e["type"] == "run_end" for e in events):
                    break
    return events, first_at, time.monotonic()


async def current_max_event_id(client: httpx.AsyncClient, run_id: str) -> int:
    """当前已有的事件条数/最大 id —— 用来只收集"这一阶段"新产生的事件。"""
    detail = (await client.get(f"/api/runs/{run_id}")).json()
    events = detail.get("events") or []
    return max((event["id"] for event in events), default=0)


def event_types(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events]


def events_of(events: list[dict[str, Any]], type_: str) -> list[dict[str, Any]]:
    return [event for event in events if event["type"] == type_]


def stop_services(*procs: subprocess.Popen) -> None:
    for proc in procs:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
