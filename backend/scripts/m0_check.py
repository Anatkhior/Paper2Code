"""M0 离线验收：不需要任何真实 API key，就能证明整条链路是通的。

它检查五件事：
1. 跨 chunk 拼接的工具调用参数能被正确还原（后端最容易写错的地方）
2. §4 启动自检的四条分支都给出人话诊断
3. /api/analyze → SSE 的完整事件链，且事件是**边跑边推**的（不是跑完一次性吐出来）
4. run 结束后才连上来的客户端能拿到完整回放，而且**不会挂住**
5. Last-Event-ID 断线重连能补发历史 + events.jsonl 落盘一致

运行（会自己拉起两个本地服务，用完自动关掉）：
    cd backend && .venv/bin/python -m scripts.m0_check
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
MOCK_PORT = 8231
APP_PORT = 8232
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}/v1"
APP = f"http://127.0.0.1:{APP_PORT}"

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(("✅ " if condition else "❌ ") + message, flush=True)
    if not condition:
        failures.append(message)


def provider(model: str, key: str = "mock-key") -> dict[str, Any]:
    return {
        "protocol": "openai-compatible",
        "base_url": MOCK_BASE,
        "api_key": key,
        "model": model,
    }


def start_service(module: str, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            module,
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
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
    stop_on_run_end: bool,
    timeout_s: float = 90.0,
) -> tuple[list[dict[str, Any]], float, float]:
    """读一条 SSE 流，返回 (事件列表, 首事件时刻, 结束时刻)。"""
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
                        if line.startswith("data:"):
                            events.append(json.loads(line[5:].strip()))
                if stop_on_run_end and any(e["type"] == "run_end" for e in events):
                    break
    return events, first_at, time.monotonic()


async def main() -> int:
    mock = start_service("devtools.mock_provider:app", MOCK_PORT)
    app_proc = start_service("app.main:app", APP_PORT)
    try:
        await wait_http(f"http://127.0.0.1:{MOCK_PORT}/health")
        await wait_http(f"{APP}/api/health")
        print(f"mock provider  : {MOCK_BASE}\nPaperLens API  : {APP}\n", flush=True)

        async with httpx.AsyncClient(timeout=120, base_url=APP) as client:
            # ---------------- 1 & 2. 启动自检及其失败分支 ----------------
            print("── §4 启动自检（走真实 HTTP 接口）──", flush=True)

            async def smoke(payload: dict[str, Any]) -> dict[str, Any]:
                response = await client.post(f"{APP}/api/provider/smoke-test", json=payload)
                return response.json()

            ok_result = await smoke(provider("mock-model"))
            check(ok_result["ok"], f"正常端点通过自检（{ok_result['diagnosis']}）")
            check(
                ok_result["steps"][0]["tool_calls"][0]["arguments"] == {"page": 1},
                f"跨 chunk 拼接的参数被正确还原：{ok_result['steps'][0]['tool_calls']}",
            )
            check(ok_result["capabilities"].get("usage_in_stream") is True, "读到端点上报的 usage")
            check(ok_result["capabilities"].get("tool_calling") is True, "capabilities 标记了 tool_calling")

            no_tools = await smoke(provider("no-tools"))
            check(not no_tools["ok"] and "工具调用" in no_tools["diagnosis"], f"不支持工具调用 → {no_tools['diagnosis']}")

            bad_args = await smoke(provider("bad-args"))
            check(not bad_args["ok"] and "参数" in bad_args["diagnosis"], f"参数不可靠 → {bad_args['diagnosis']}")

            bad_key = await smoke(provider("mock-model", key="wrong-key"))
            check(not bad_key["ok"] and "认证失败" in bad_key["diagnosis"], f"错误 key → {bad_key['diagnosis']}")

            rejected = await client.post(f"{APP}/api/provider/smoke-test", json=provider("mock-model", key=""))
            check(rejected.status_code == 422, "空 api_key 在入口被 422 拒绝，不会浪费一次网络请求")

            # ---- HTML 错误页诊断（回归：不能把整页 HTML 甩给用户）----
            html_blocked = await smoke(provider("html-error"))
            check(
                not html_blocked["ok"] and "Cloudflare" in html_blocked["diagnosis"],
                f"网关返回 HTML 错误页时说人话（{html_blocked['diagnosis'][:56]}…）",
            )
            check(
                "<!DOCTYPE" not in html_blocked["diagnosis"] and "<html" not in html_blocked["diagnosis"],
                "不会把整页 HTML 原样丢给用户（这曾经是真 bug）",
            )
            check(
                "PAPERLENS_HTTP_USER_AGENT" in html_blocked["diagnosis"],
                "诊断里给出 UA 逃生开关（有些网关按 UA 拦 Python SDK，实测于 2026-09-13）",
            )
            check(
                bool(html_blocked.get("diagnostics", {}).get("probes")),
                "失败时顺手探测端点，好让用户分清'网关坏了'和'地址写错了'",
            )

            # ---------------- 2.5 单次调用超时与端点探测 ----------------
            print("\n── §8 单次调用超时 + 端点探测 + 客户端 UA ──", flush=True)
            from app.config import settings as app_settings
            from app.providers import (
                ProviderConfig,
                _diagnose_exception,
                _probe_urls,
                probe_endpoint,
                stream_turn,
            )

            # ---- 客户端 UA 可控（第三方网关兼容性，2026-09-13）----
            # 实测：有套 Cloudflare 的网关按 User-Agent 把 openai-python SDK 形状的请求
            # 直接 403（请求根本到不了模型）。默认 UA 已改为自定义的 PaperLens/0.1。
            # mock 的 ua-check 会把收到的 UA 原样回显，以此断言自定义 UA 真的
            # 穿过 litellm/openai 客户端到达端点（而不是只改了配置没生效）。
            ua_turn = await stream_turn(
                ProviderConfig(**provider("ua-check")),
                [{"role": "user", "content": "hi"}],
                None,
            )
            check(
                ua_turn.text == f"UA={app_settings.http_user_agent}",
                f"LLM 请求带上自定义 User-Agent：{ua_turn.text[:60]}",
            )
            ua_probe = await probe_endpoint(ProviderConfig(**provider("ua-check")))
            first_probe = (ua_probe.get("probes") or [{}])[0]
            check(
                str(app_settings.http_user_agent) in str(first_probe.get("snippet", "")),
                "端点探测请求带同一个 User-Agent",
            )

            # _probe_urls 是纯函数：base 带 /v1 时只有一个候选（曾经会把同一个 URL 探测两遍），
            # 不带 /v1 时补一个 /v1 候选（"漏了 /v1"是最常见的 base_url 手误）。
            with_v1 = _probe_urls("https://api.example.com/v1")
            without_v1 = _probe_urls("https://api.example.com")
            check(
                with_v1 == ["https://api.example.com/v1/models"],
                f"base 以 /v1 结尾 → 只探测一个地址（去重）：{with_v1}",
            )
            check(
                without_v1 == ["https://api.example.com/models", "https://api.example.com/v1/models"],
                f"base 没带 /v1 → 补探测 /v1/models：{without_v1}",
            )

            # per_turn_timeout_seconds 必须真的接到了 stream_turn 上：
            # 把上限拨到 1s，让 slow-model（第一轮响应前停 1.2s）必然超时，
            # 超时还得被翻译成人话，而不是一堆 SDK 异常原文。
            cfg_slow = ProviderConfig(**provider("slow-model"))
            ping_messages = [
                {"role": "system", "content": "你必须使用提供的工具来回答问题，不要凭空回答。"},
                {"role": "user", "content": "请调用 ping 工具，page 参数填 1。"},
            ]
            original_timeout = app_settings.per_turn_timeout_seconds
            app_settings.per_turn_timeout_seconds = 1
            try:
                started_at = time.monotonic()
                try:
                    await stream_turn(cfg_slow, ping_messages, None)
                    check(False, "慢端点在 1s 时限内应该被打断（却正常返回了）")
                except Exception as exc:  # noqa: BLE001
                    elapsed = time.monotonic() - started_at
                    text = str(exc).lower()
                    # 核心语义是「必须抛超时、而不是等 mock 1.2s 后正常返回」；
                    # litellm 内部可能对超时重试几次，所以上界只防"挂死"，不卡精确耗时。
                    check(
                        elapsed < 20 and ("timed out" in text or "timeout" in text),
                        f"per_turn_timeout_seconds 生效：{elapsed:.1f}s 时被打断（{type(exc).__name__}）",
                    )
                    check(
                        "超时" in _diagnose_exception(exc),
                        f"超时被翻译成人话：{_diagnose_exception(exc)[:60]}",
                    )
            finally:
                app_settings.per_turn_timeout_seconds = original_timeout

            # ---------------- 3. 完整事件链，且必须边跑边推 ----------------
            print("\n── /api/analyze → SSE 事件链 ──", flush=True)
            created = (await client.post(f"{APP}/api/analyze", json={"provider": provider("mock-model")})).json()
            run_id = created["run_id"]
            print(f"   run_id = {run_id}", flush=True)

            events, first_at, end_at = await collect_sse(client, f"/api/runs/{run_id}/events", stop_on_run_end=True)
            types = [event["type"] for event in events]
            print("   事件序列：" + " → ".join(types), flush=True)
            print(f"   首事件比 run_end 早到 {end_at - first_at:.3f}s（说明是流式推送，不是跑完一次性吐出）", flush=True)

            check(end_at - first_at > 0.3, "事件是流式推送的，不是一次性批量返回")
            check(types and types[0] == "run_start", "第一条事件是 run_start")
            check("step_start" in types, "收到 step_start")
            check(types.count("tool_call") == 2, f"收到 {types.count('tool_call')} 次 tool_call（ping + finish）")
            check(types.count("tool_result") == types.count("tool_call"), "每次 tool_call 都有对应的 tool_result")
            check("assistant_text" in types, "收到 assistant_text 增量（前端打字机输出靠它）")
            check(types[-1] == "run_end", "最后一条事件是 run_end")

            end = next((e for e in events if e["type"] == "run_end"), None)
            data = (end or {}).get("data", {})
            check(data.get("status") == "ok", f"run 正常结束：status={data.get('status')}")
            check(data.get("usage", {}).get("tool_calls") == 2, "预算记账正确（tool_calls = 2）")
            check(data.get("usage", {}).get("input_tokens", 0) > 0, "预算记账正确（input_tokens > 0）")
            check(data.get("stopped_reason") == "Agent 主动结束", f"结束原因：{data.get('stopped_reason')}")
            check(bool(data.get("coverage_note")), f"finish 的 coverage_note 被记录下来：{data.get('coverage_note')!r}")

            # ---------------- 4 & 5. 回放、落盘、断线重连 ----------------
            print("\n── 回放 / 落盘 / 断线重连 ──", flush=True)
            detail = (await client.get(f"{APP}/api/runs/{run_id}")).json()
            check(detail["finished"] is True, "run 结束后 /api/runs/{id} 标记 finished")
            check(len(detail["events"]) == len(events), f"events.jsonl 落盘条数一致（{len(detail['events'])}）")

            # 关键回归用例：run 已经结束之后才连上来，必须拿到完整回放且**不能挂住**
            replay, _, _ = await collect_sse(
                client, f"/api/runs/{run_id}/events", stop_on_run_end=False, timeout_s=15
            )
            check(len(replay) == len(events), f"结束后新连接能拿到完整回放（{len(replay)} 条），且连接会正常关闭")

            resumed, _, _ = await collect_sse(
                client, f"/api/runs/{run_id}/events?from_id=2", stop_on_run_end=False, timeout_s=15
            )
            check(
                bool(resumed) and resumed[0]["id"] == 3 and len(resumed) == len(events) - 2,
                f"按 id 续传正确（从 id={resumed[0]['id'] if resumed else None} 开始，共 {len(resumed)} 条）",
            )

        # ---------------- 6. 后端重启后的回放必须收尾 ----------------
        # 回归（2026-09-12）：重启后 _registry 从磁盘恢复 run，内存里 task=None。
        # SSE 收尾条件原来是「task.done()」，对恢复出来的 run 永远不成立，
        # 客户端回放完历史会被挂在半空。正确条件是「task 为 None 或已结束」。
        print("\n── 后端重启后的回放收尾 ──", flush=True)
        app_proc.terminate()
        try:
            app_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            app_proc.kill()
        app_proc = start_service("app.main:app", APP_PORT)
        await wait_http(f"{APP}/api/health")
        async with httpx.AsyncClient(timeout=120, base_url=APP) as client2:
            try:
                replay_after_restart, _, _ = await collect_sse(
                    client2, f"/api/runs/{run_id}/events", stop_on_run_end=False, timeout_s=15
                )
                check(
                    bool(replay_after_restart) and replay_after_restart[-1]["type"] == "run_end",
                    f"后端重启后能完整回放（{len(replay_after_restart)} 条），且回放完服务端主动收尾",
                )
            except TimeoutError:
                check(False, "后端重启后回放挂住了（15s 内流没有结束）——SSE 收尾条件回归失败")

        print(flush=True)
        if failures:
            print(f"❌ M0 验收未通过，{len(failures)} 项失败：", flush=True)
            for item in failures:
                print(f"   - {item}", flush=True)
            return 1
        print("✅ M0 验收全部通过：BYOK 抽象、启动自检、SSE 事件契约、流式推送、预算记账、回放与续传、落盘。", flush=True)
        return 0
    finally:
        for proc in (mock, app_proc):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
