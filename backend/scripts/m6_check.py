"""阶段②（v1）离线验收：在原文里划选 → 加为定位目标。

用户的诉求是"我自己指定想看论文哪一部分"。这一阶段要保证三件事：
A. 清单是**可编辑状态**：能加、能改、能删，没跑过侦察也能加；用户划选的原文同样走引文核验。
B. 加进来的目标**真的会被定位**：不跑侦察、只有用户目标时，阶段 B 也能跑出结论。
C. 界面上有入口：划选提示常驻、清单里能区分"你添加的"目标。

运行：
    cd backend && .venv/bin/python -m scripts.m6_check
    PAPERLENS_SKIP_FRONTEND=1 ...   # 只跑接口与端到端，跳过前端构建
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
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
FRONTEND_PORT = 3301

QUOTE_P3 = "Low-rank reparameterization reduces the number of trainable parameters by four orders of magnitude."
QUOTE_P4 = "The forward pass scales the low-rank branch by alpha / r, which keeps the magnitude of the update roughly constant."
QUOTE_NOWHERE = "We schedule the learning rate with a cosine decay and warm up for the first thousand steps."


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _partial_quote(real_quote: str) -> str:
    """造一条「真开头 + 编造尾巴」的引文：折叠空白后前 60 个字符与原文一致，后面是编的。"""
    prefix = next(
        real_quote[: cut + 1] for cut in range(len(real_quote)) if len(_squash(real_quote[: cut + 1])) >= 60
    )
    return prefix + " fabricated tail that never appears anywhere in the paper."


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


async def new_run(client: httpx.AsyncClient, check: Checker, model: str = "mock-model") -> str:
    ensure_fixtures(ROOT)
    pdf = (ROOT / "tests" / "fixtures" / "synthetic_paper.pdf").read_bytes()
    response = await client.post(
        "/api/runs",
        files={"file": ("synthetic_paper.pdf", pdf, "application/pdf")},
        data={"provider": json.dumps(provider(model))},
    )
    check(response.status_code == 200, f"上传论文（HTTP {response.status_code}）")
    return response.json()["run_id"]


# ---------------------------------------------------------------------------
# A. 清单的可编辑性
# ---------------------------------------------------------------------------
async def section_a(check: Checker, client: httpx.AsyncClient) -> str:
    check.section("A. 清单接口：加 / 改 / 删 / 核验")
    run_id = await new_run(client, check)

    # 没跑侦察也能加
    added = (await client.post(f"/api/runs/{run_id}/plan/items", json={"page": 3, "quote": QUOTE_P3})).json()
    item = added["added"]
    check(item["id"] == "user-1", f"没跑过侦察也能加目标（id={item['id']}）")
    check(item["source"] == "user", "条目被标记为 source=user")
    check(
        item["paper_evidence"][0]["verified"] is True,
        "从第 3 页划选的原文通过了引文核验",
    )
    check(item["name"].startswith("Low-rank"), f"没给名字时用原文开头兜底：{item['name']!r}")
    check(item["search_hints"] == [], "纯散文挑不出代码标识符时，线索留空（不塞废词让 Agent 自己判断）")

    # 含标识符的原文能自动挑出线索
    added2 = (
        await client.post(
            f"/api/runs/{run_id}/plan/items",
            json={
                "page": 4,
                "quote": QUOTE_P4,
                "name": "缩放系数怎么算的",
                "search_hints": ["scaling", "lora_alpha"],
            },
        )
    ).json()["added"]
    check(added2["id"] == "user-2", "第二个目标 id 递增")
    check(added2["search_hints"] == ["scaling", "lora_alpha"], "用户给的线索被保留")

    # 引文核验：页码说错 → 不拒绝，但如实标记
    wrong_page = (
        await client.post(f"/api/runs/{run_id}/plan/items", json={"page": 5, "quote": QUOTE_P3})
    ).json()["added"]
    check(
        wrong_page["paper_evidence"][0]["verified"] is False,
        "把第 3 页的原文说成第 5 页 → 不拒绝用户输入，但标记 verified=false",
    )

    # 改名 / 删除
    renamed = (await client.patch(f"/api/runs/{run_id}/plan/items/user-2", json={"name": "缩放到底怎么算"})).json()
    names = {i["id"]: i["name"] for i in renamed["plan"]["innovations"]}
    check(names["user-2"] == "缩放到底怎么算", "能改名")

    removed = (await client.delete(f"/api/runs/{run_id}/plan/items/{wrong_page['id']}")).json()
    remaining = [i["id"] for i in removed["plan"]["innovations"]]
    check(remaining == ["user-1", "user-2"], f"能删除（剩 {remaining}）")

    # 持久化：换一个"客户端"重新拉，仍在（模拟刷新页面）
    fetched = (await client.get(f"/api/runs/{run_id}/plan")).json()
    check(
        [i["id"] for i in fetched["plan"]["innovations"]] == ["user-1", "user-2"],
        "清单落在服务端（刷新页面不会丢自己加的目标）",
    )
    check(fetched["user_items"] == 2, f"统计到 {fetched['user_items']} 条用户目标")

    # 护栏
    empty = await client.post(f"/api/runs/{run_id}/plan/items", json={})
    check(empty.status_code == 422, f"既没名字也没原文 → 422（HTTP {empty.status_code}）")
    missing_patch = await client.patch(f"/api/runs/{run_id}/plan/items/user-99", json={"name": "x"})
    check(missing_patch.status_code == 404, f"改不存在的 id → 404（HTTP {missing_patch.status_code}）")
    unknown_run = await client.get("/api/runs/no-such-run/plan")
    check(unknown_run.status_code == 404, f"未知 run → 404（HTTP {unknown_run.status_code}）")

    # 「真开头 + 编造尾巴」的引文：通过核验（verified=true）但必须标成 partial，
    # 不能在界面上冒充逐字引用（回归，2026-09-12）
    partial_added = (
        await client.post(f"/api/runs/{run_id}/plan/items", json={"page": 3, "quote": _partial_quote(QUOTE_P3)})
    ).json()["added"]
    partial_ev = partial_added["paper_evidence"][0]
    check(
        partial_ev["verified"] is True and partial_ev["quote_match"] == "partial",
        f"只匹配到开头的引文 → 如实标注 partial（verified={partial_ev['verified']}，"
        f"quote_match={partial_ev['quote_match']}）",
    )

    # ---- 阶段运行中不许改清单（patch/delete 的 409 守卫，2026-09-12 回归）----
    # add 一直有这个守卫，patch/delete 漏了：定位跑着的时候改清单，
    # 改动不会生效（targets 已快照）却没有任何提示。slow-model 第一轮响应前停 1.2s，
    # 保证下面的请求一定落在运行窗口内，断言不靠碰运气。
    baseline = await current_max_event_id(client, run_id)
    recon = await client.post(f"/api/runs/{run_id}/recon", json={"provider": provider("slow-model")})
    check(recon.status_code == 200, "slow-model 侦察已启动")
    guarded_patch = await client.patch(f"/api/runs/{run_id}/plan/items/user-2", json={"name": "运行中改名"})
    check(guarded_patch.status_code == 409, f"阶段运行中 PATCH 清单 → 409（HTTP {guarded_patch.status_code}）")
    guarded_delete = await client.delete(f"/api/runs/{run_id}/plan/items/user-2")
    check(guarded_delete.status_code == 409, f"阶段运行中 DELETE 清单 → 409（HTTP {guarded_delete.status_code}）")
    guarded_add = await client.post(f"/api/runs/{run_id}/plan/items", json={"name": "运行中加的"})
    check(guarded_add.status_code == 409, f"阶段运行中 ADD 清单 → 409（既有守卫，一并回归）")
    await collect_sse(client, f"/api/runs/{run_id}/events?from_id={baseline}", since_id=baseline)

    # ---- 用户加的目标不能被侦察结果冲掉（回归，2026-09-12）----
    # 侦察是在本 run 先加了 3 条用户目标之后才跑的：record_plan 提交时必须把
    # 已有的 user 条目并进新清单，否则用户先划选、再侦察，目标会被静默清空。
    after_recon = (await client.get(f"/api/runs/{run_id}/plan")).json()["plan"]
    ids = [item["id"] for item in after_recon["innovations"]]
    check(
        {"user-1", "user-2", partial_added["id"]} <= set(ids) and len(ids) == 6,
        f"侦察后清单保留全部用户目标（{ids}）",
    )
    after_patch = await client.patch(f"/api/runs/{run_id}/plan/items/user-2", json={"name": "结束后改名"})
    check(after_patch.status_code == 200, "阶段结束后恢复正常编辑")

    return run_id


# ---------------------------------------------------------------------------
# B. 只有用户目标时也能定位
# ---------------------------------------------------------------------------
async def section_b(check: Checker, client: httpx.AsyncClient) -> None:
    check.section("B. 端到端：只有你自己指定的目标，也能定位")
    run_id = await new_run(client, check)
    await client.post(f"/api/runs/{run_id}/plan/items", json={"page": 4, "quote": QUOTE_P4, "search_hints": ["scaling", "lora_alpha"]})
    await client.post(
        f"/api/runs/{run_id}/plan/items",
        json={"page": 5, "quote": QUOTE_NOWHERE, "name": "学习率调度", "search_hints": ["cosine_schedule"]},
    )

    baseline = await current_max_event_id(client, run_id)
    started = await client.post(
        f"/api/runs/{run_id}/locate",
        json={"provider": provider("mock-model"), "repo_url": str(build_repo(ROOT))},
    )
    check(started.status_code == 200, f"没跑侦察、只有用户目标 → 阶段 B 能启动（HTTP {started.status_code}）")
    events, _, _ = await collect_sse(client, f"/api/runs/{run_id}/events?from_id={baseline}", since_id=baseline)

    calls = [event["data"]["tool"] for event in events_of(events, "tool_call")]
    check("search_code" in calls and "read_file" in calls, f"用户目标也走了'搜 → 读'的流程：{calls}")
    check(calls.count("record_finding") >= 2, f"两个目标各给了一次结论（{calls.count('record_finding')} 次）")

    artifact = (await client.get(f"/api/runs/{run_id}")).json()["artifact"]
    ids = [item["id"] for item in artifact["innovations"]]
    check(ids == ["user-1", "user-2"], f"产物里是用户目标（{ids}）")
    check(artifact["missing_ids"] == [], "两个目标都有结论，没有漏掉")

    found = [item for item in artifact["innovations"] if item["status"] != "not_found"]
    missing = [item for item in artifact["innovations"] if item["status"] == "not_found"]
    check(len(found) >= 1, f"有目标找到了实现（{len(found)} 条）")
    check(
        all(
            evidence["verification"]["state"] == "verified"
            for item in found
            for evidence in item["code_evidence"]
        ),
        "用户目标的代码引用同样经过机械核验",
    )
    check(len(missing) >= 1, f"搜不到的那个诚实报了 not_found（{len(missing)} 条）")
    check(
        bool(missing and missing[0]["searched"]),
        f"not_found 条目带着搜索记录：{missing[0]['searched'] if missing else None}",
    )
    check(
        artifact["verification"]["citation_verifiable_rate"] == 1.0,
        f"引用核验率 {artifact['verification']['citation_verifiable_rate']}",
    )


# ---------------------------------------------------------------------------
# C. 界面入口
# ---------------------------------------------------------------------------
async def section_c(check: Checker) -> None:
    check.section("C. 界面：划选入口与'你添加的'标记")
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
            "以此为目标定位代码": "划选后的动作按钮",
            "划选一段原文": "划选提示（常驻可见）",
            "你添加的": "用户目标标记的说明",
            # 回归：按钮旁边必须写清"还差什么"，并且反馈条是 sticky 的。
            # 起因是用户点「开始定位」没反应——唯一反馈渲染在页面顶部屏幕外，等于静默失败。
            "还差：": "按钮旁边写清缺什么（防静默失败）",
        }.items():
            check(marker in html, f"页面里有「{description}」（{marker}）")

        # 错误提示条只在出错时渲染，静态 HTML 里看不到，所以去打包产物里确认它在
        # —— 这是"点了没反应"那个 bug 的守卫：反馈必须吸附在可视区域，不能又跑回页面顶部。
        bundle_hit = any(
            "sticky top-2 z-50" in chunk.read_text(encoding="utf-8", errors="ignore")
            for chunk in (FRONTEND / ".next" / "static" / "chunks").rglob("*.js")
        )
        check(bundle_hit, "错误提示是 sticky 的（反馈留在用户正在看的地方）")

        # partial 引文必须有自己的展示形态，不能看起来和逐字核验通过一模一样
        partial_hit = any(
            "部分匹配" in chunk.read_text(encoding="utf-8", errors="ignore")
            for chunk in (FRONTEND / ".next" / "static" / "chunks").rglob("*.js")
        )
        check(partial_hit, "「引文部分匹配」徽章进了打包产物（partial 不能冒充逐字核验）")

        # 仓库就绪行的提示只在 repo_ready 事件到来时渲染，静态 HTML 里同样看不到：
        # 系统替用户做的决定（哪些文件没下载、地址判定为什么放行）必须显示出来。
        notes_hit = any(
            "未下载" in chunk.read_text(encoding="utf-8", errors="ignore")
            for chunk in (FRONTEND / ".next" / "static" / "chunks").rglob("*.js")
        )
        check(notes_hit, "仓库就绪行显示「另有 N 个未下载」（源码视图克隆跳过了什么，用户看得见）")

        # 阅读器（2026-09-16 用户反馈后重做）：
        #   ① 原版页面视图 = 服务端渲染图 + 我们自己叠的高亮框（浏览器内置阅读器不让脚本碰 DOM，
        #      `#search=` 在 Chrome 上不生效，所以只能自己画）；
        #   ② 时间线/追问的"自动跟到最新"只滚自己的容器（scrollHeight/clientHeight），
        #      不再用 scrollIntoView 把整个窗口拽走。
        chunks = list((FRONTEND / ".next" / "static" / "chunks").rglob("*.js"))
        texts = [chunk.read_text(encoding="utf-8", errors="ignore") for chunk in chunks]
        check(
            any("highlight_rects" in text for text in texts),
            "原版页面视图：高亮框数据（highlight_rects）进了打包产物",
        )
        check(
            any("scrollHeight" in text and "clientHeight" in text for text in texts),
            "自动滚动改为容器内跟随（scrollHeight/clientHeight），不再动窗口",
        )
        check(
            not any('block:"end"' in text for text in texts),
            "旧的 window-stealing 写法（scrollIntoView block:end）已从产物里消失",
        )
        # 追问回答与结论解释都是 markdown，必须经渲染器（自写的安全渲染器，类名 md-list 是它的标记）。
        # 它只被 ChatPanel / ComparePanel 引用：出现在产物里就说明这条链路真的接上了。
        check(
            any("md-list" in text for text in texts),
            "markdown 渲染器进了打包产物（回答不再把 **粗体** 这类标记原样显示）",
        )
        # 自检通过时只留一句人话（capabilities JSON 挪到失败时才显示）
        check(
            any("smoke-compact" in text for text in texts),
            "自检通过的紧凑样式进了打包产物（成功后不再摆 capabilities JSON）",
        )
        # 布局：创新点勾选与逐条结论都用多列网格（用户反馈单列太浪费空间）
        check(
            any("plan-list" in text and "findings-grid" in text for text in texts),
            "多列网格进产物：创新点勾选区（.plan-list）与逐条结论（.findings-grid）",
        )
        check(
            any("xl:grid-cols-2" in text for text in texts),
            "逐条结论在宽屏下是两列（xl:grid-cols-2）",
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()


async def main() -> int:
    check = Checker("阶段②（划选→定位目标）验收")
    mock = start_service("devtools.mock_provider:app", MOCK_PORT)
    app_proc = start_service(
        "app.main:app", APP_PORT, extra_env={"PAPERLENS_ALLOW_LOCAL_REPO_PATHS": "true"}
    )
    try:
        await wait_http(f"http://127.0.0.1:{MOCK_PORT}/health")
        await wait_http(f"{APP}/api/health")
        print(f"mock provider : {MOCK_PORT}\nPaperLens API : {APP}", flush=True)

        async with httpx.AsyncClient(base_url=APP, timeout=300) as client:
            await section_a(check, client)
            await section_b(check, client)

        await section_c(check)
        return check.finish()
    finally:
        stop_services(mock, app_proc)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
