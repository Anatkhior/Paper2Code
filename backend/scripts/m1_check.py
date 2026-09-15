"""M1 离线验收：阶段 A（侦察）整条链路。

不需要任何真实 API key。分四段：

A. 论文层与工具护栏（不经过 LLM，直接调工具）
   —— 引文核验、页码越界、超长截断、全文超限、路径穿越、plan schema 校验与"一次改正机会"
B. 端到端：上传 PDF → recon → 清单产出（走真实 HTTP + SSE）
C. 端到端：故意写错引文页码 → 被后端打回 → 修正后通过（诚实性机制）
D. 上传护栏：非 PDF / 超大 / 页数超限 / provider 配置非法 / 未知 run

运行：
    cd backend && .venv/bin/python -m scripts.m1_check
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import re
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
    events_of,
    provider,
    start_service,
    stop_services,
    wait_http,
    ROOT,
)

from tests.paper_fixture import INIT_QUOTE, KEY_QUOTE, ensure_fixtures


async def expect_error(check: Checker, thunk: Any, message: str, contains: str = "") -> None:
    """断言这个调用会抛异常——用来测护栏。"""
    from app.agent.tools.base import ToolError

    try:
        result = thunk()
        if inspect.isawaitable(result):
            result = await result
        check(False, f"{message}（没有抛错，返回了 {str(result)[:80]}）")
    except ToolError as exc:
        ok = contains in str(exc) if contains else True
        check(ok, f"{message} → {str(exc)[:100]}")
    except Exception as exc:  # noqa: BLE001
        check(False, f"{message}（抛了非预期的 {type(exc).__name__}: {str(exc)[:80]}）")


# ---------------------------------------------------------------------------
# A. 论文层与工具护栏
# ---------------------------------------------------------------------------
async def section_a(check: Checker) -> None:
    from app.agent.tools.base import ToolContext, safe_path
    from app.agent.tools.findings import RECORD_PLAN
    from app.agent.tools.paper_tools import PAPER_TOOLS
    from app.events import RunBus
    from app.paper import PaperDocument

    check.section("A. 论文层与工具护栏（不经过 LLM）")
    small, huge = ensure_fixtures(ROOT)
    work = ROOT / "data" / f"unit-{int(time.time() * 1000)}"
    work.mkdir(parents=True, exist_ok=True)
    bus = RunBus("unit", work / "events.jsonl")
    tools = {tool.name: tool for tool in PAPER_TOOLS}

    doc = PaperDocument(small, cache_dir=work / "cache")
    ctx = ToolContext(run_id="unit", run_dir=work, bus=bus, paper=doc)

    outcome = await tools["list_pages"].handler({}, ctx)
    listing = json.loads(outcome.content)
    check(listing["page_count"] == 6, f"list_pages 报出 6 页（实际 {listing['page_count']}）")
    check(
        len(listing["pages"]) == 6 and all(row["chars"] > 0 for row in listing["pages"]),
        "每页都有字符数与首行，Agent 能据此判断结构",
    )
    check(
        all(len(row["first_line"]) <= 70 for row in listing["pages"]),
        "list_pages 不返回正文（只有截断过的首行摘要），避免白占上下文",
    )

    outcome = await tools["get_page_text"].handler({"page": 3}, ctx)
    check(KEY_QUOTE in outcome.content, "get_page_text(3) 取到方法章节原文")
    check(outcome.details["truncated"] is False, "正常页不会被截断")

    await expect_error(
        check,
        lambda: tools["get_page_text"].handler({"page": 99}, ctx),
        "页码越界被拒绝",
        contains="超出范围",
    )
    await expect_error(
        check,
        lambda: tools["get_page_text"].handler({"page": "3"}, ctx),
        "非整数页码被拒绝",
        contains="整数",
    )

    outcome = await tools["search_paper"].handler({"query": "low-rank"}, ctx)
    hits = json.loads(outcome.content)["hits"]
    check({hit["page"] for hit in hits} >= {1, 3}, f"search_paper 命中第 1、3 页（实际 {sorted({h['page'] for h in hits})}）")

    outcome = await tools["search_paper"].handler({"query": "zzz-not-a-word"}, ctx)
    check(json.loads(outcome.content)["hits"] == [], "搜不到时返回空结果并给出下一步建议，而不是报错")

    # --- 引文核验 ---
    check(doc.quote_found(3, KEY_QUOTE) is True, "第 3 页的真实引文 → 核验通过")
    check(doc.quote_found(5, KEY_QUOTE) is False, "把第 3 页的引文说成第 5 页 → 核验失败（防编造）")
    check(doc.quote_found(3, "too short") is False, "过短的引文不算有效证据")
    check(doc.quote_found(3, INIT_QUOTE) is False, "第 4 页的引文放到第 3 页 → 核验失败")

    # --- 引文匹配分级（回归，2026-09-12）---
    # quote_found 对超长引文只要求"前 60 个折叠字符匹配"，以前不管后面是不是编的都算通过，
    # 还承诺"由调用方标注为部分匹配"——但没有任何调用方实现。现在分级由 quote_match 给出，
    # partial 必须被如实标出来，不能让「真开头 + 编造后半段」冒充逐字引用。
    def _squash(text: str) -> str:
        return re.sub(r"\s+", "", text)

    partial_prefix = next(
        KEY_QUOTE[: cut + 1] for cut in range(len(KEY_QUOTE)) if len(_squash(KEY_QUOTE[: cut + 1])) >= 60
    )
    partial_quote = partial_prefix + " fabricated tail that never appears anywhere in the paper."
    check(doc.quote_match(3, KEY_QUOTE) == "full", "quote_match：逐字引用 → full")
    check(
        doc.quote_match(3, partial_quote) == "partial",
        "quote_match：真开头 + 编造尾巴 → partial（不再无声冒充逐字引用）",
    )
    check(doc.quote_found(3, partial_quote) is True, "quote_found 兼容：partial 仍算「找到」（打回机制不变）")
    check(doc.quote_match(3, "an entirely invented sentence") == "none", "quote_match：整句编造 → none")
    doc.close()

    # --- 大文件护栏 ---
    big = PaperDocument(huge, cache_dir=work / "cache2")
    ctx_big = ToolContext(run_id="unit-big", run_dir=work, bus=bus, paper=big)
    outcome = await tools["get_page_text"].handler({"page": 1}, ctx_big)
    check(
        outcome.details["truncated"] is True and "[已截断" in outcome.content,
        "超长页被截断，并明确标注 [已截断]（不静默丢内容）",
    )
    await expect_error(
        check,
        lambda: tools["read_paper_all"].handler({}, ctx_big),
        "全文超过单次上限时给出明确错误",
        contains="超过单次上限",
    )
    big.close()

    # --- 路径穿越 ---
    await expect_error(
        check,
        lambda: safe_path(work, "../../etc/passwd"),
        "路径穿越被拒绝",
        contains="越界",
    )

    # --- record_plan：schema 校验 + 引文打回 + 一次改正机会 ---
    doc2 = PaperDocument(small, cache_dir=work / "cache")
    plan_ctx = ToolContext(run_id="unit-plan", run_dir=work, bus=bus, paper=doc2)
    from devtools.mock_provider import BAD_PLAN, RECON_PLAN

    await expect_error(
        check,
        lambda: RECORD_PLAN.handler({"paper_summary": "x", "innovations": [{"id": "a"}]}, plan_ctx),
        "缺少必填字段的 plan 被打回，并指出具体哪个字段",
        contains="不合格",
    )
    await expect_error(
        check,
        lambda: RECORD_PLAN.handler(BAD_PLAN, plan_ctx),
        "引文页码写错的 plan 第一次被打回",
        contains="找不到",
    )
    outcome = await RECORD_PLAN.handler(BAD_PLAN, plan_ctx)
    first_evidence = plan_ctx.state["plan"]["innovations"][0]["paper_evidence"][0]
    check(
        outcome.terminate is True and first_evidence["verified"] is False,
        "第二次仍不合格 → 接受但标记 verified=false（给它改正机会，但不许无限循环）",
    )

    plan_ctx2 = ToolContext(run_id="unit-plan2", run_dir=work, bus=bus, paper=doc2)
    outcome = await RECORD_PLAN.handler(copy.deepcopy(RECON_PLAN), plan_ctx2)
    verified = [
        ev["verified"]
        for inn in plan_ctx2.state["plan"]["innovations"]
        for ev in inn["paper_evidence"]
    ]
    check(outcome.terminate is True, "plan 合格时 record_plan 会终止本次运行")
    check(all(v is True for v in verified), f"全部引文核验通过（{verified}）")
    graded = [
        ev["quote_match"]
        for inn in plan_ctx2.state["plan"]["innovations"]
        for ev in inn["paper_evidence"]
    ]
    check(all(g == "full" for g in graded), f"逐字引用的匹配分级都是 full（{graded}）")
    check(len(plan_ctx2.state["plan"]["innovations"]) == 3, "plan 里有 3 条创新点")

    # partial 引用：被接受，但 evidence 上必须带着 quote_match=partial 的标记
    plan_ctx3 = ToolContext(run_id="unit-plan3", run_dir=work, bus=bus, paper=doc2)
    partial_plan = copy.deepcopy(RECON_PLAN)
    crafted = partial_plan["innovations"][0]["paper_evidence"][0]
    crafted["page"] = 3
    crafted["quote"] = partial_quote
    await RECORD_PLAN.handler(partial_plan, plan_ctx3)
    marked = plan_ctx3.state["plan"]["innovations"][0]["paper_evidence"][0]
    check(
        marked["verified"] is True and marked["quote_match"] == "partial",
        f"partial 引用被接受但如实标注（verified={marked['verified']}，quote_match={marked['quote_match']}）",
    )
    doc2.close()


# ---------------------------------------------------------------------------
# B / C. 端到端：上传 → recon
# ---------------------------------------------------------------------------
async def upload_paper(client: httpx.AsyncClient, check: Checker, model: str) -> str:
    pdf = (ROOT / "tests" / "fixtures" / "synthetic_paper.pdf").read_bytes()
    response = await client.post(
        "/api/runs",
        files={"file": ("synthetic_paper.pdf", pdf, "application/pdf")},
        data={"provider": json.dumps(provider(model))},
    )
    check(response.status_code == 200, f"上传论文成功（HTTP {response.status_code}）")
    payload = response.json()
    check(payload["paper"]["page_count"] == 6, f"解析出 6 页（实际 {payload['paper']['page_count']}）")
    check(len(payload["paper"]["sha256"]) == 64, "记录了 PDF 的 sha256（后续做幂等键用）")
    check(payload["provider"].startswith("openai-compatible"), f"provider 已脱敏描述：{payload['provider']}")
    return payload["run_id"]


async def run_recon(client: httpx.AsyncClient, check: Checker, run_id: str, model: str) -> list[dict[str, Any]]:
    started = await client.post(f"/api/runs/{run_id}/recon", json={"provider": provider(model)})
    check(started.status_code == 200, f"阶段 A 已启动（HTTP {started.status_code}）")
    events, first_at, end_at = await collect_sse(client, f"/api/runs/{run_id}/events")
    print(f"   事件序列：{' → '.join(e['type'] for e in events)}", flush=True)
    print(f"   流式推送跨度 {end_at - first_at:.2f}s", flush=True)
    check(end_at - first_at > 0.3, "事件是边跑边推的，不是跑完一次性返回")
    return events


async def section_b(check: Checker, client: httpx.AsyncClient) -> None:
    check.section("B. 端到端：上传 → 侦察 → 清单")

    run_id = await upload_paper(client, check, "mock-model")
    events = await run_recon(client, check, run_id, "mock-model")
    types = [event["type"] for event in events]

    check(types[0] == "paper_ready", "第一条事件是 paper_ready（上传阶段就发出来了）")
    check(types[-1] == "run_end", "最后一条事件是 run_end")

    calls = events_of(events, "tool_call")
    names = [call["data"]["tool"] for call in calls]
    check(names == ["list_pages", "get_page_text", "record_plan"], f"工具调用顺序符合预期：{names}")
    check(
        len(events_of(events, "tool_result")) == len(calls),
        "每次 tool_call 都有对应的 tool_result",
    )
    run_start = events_of(events, "run_start")[0]
    check(run_start["data"]["meta"]["phase"] == "recon", "run_start 标明了阶段 recon")

    plans = events_of(events, "plan_ready")
    check(len(plans) == 1, "收到 1 次 plan_ready")
    plan = plans[0]["data"]["plan"]
    check(len(plan["innovations"]) == 3, f"清单里有 3 条创新点（实际 {len(plan['innovations'])}）")
    check(bool(plan["paper_summary"]), "plan 带论文摘要")
    check(
        all(inn["search_hints"] for inn in plan["innovations"]),
        "每条创新点都带了 search_hints（阶段 B 的搜索线索）",
    )
    all_verified = [
        ev["verified"] for inn in plan["innovations"] for ev in inn["paper_evidence"]
    ]
    check(all(all_verified), f"plan_ready 里所有引文都通过核验（{all_verified}）")
    check(plans[0]["data"]["unverified_quotes"] == 0, "未核验引文数为 0")

    end = events_of(events, "run_end")[0]["data"]
    check(end["status"] == "ok", f"run 状态 ok（{end['status']}）")
    check(end["stopped_reason"] == "Agent 主动结束", f"结束原因：{end['stopped_reason']}")
    check(end["usage"]["tool_calls"] == 3, f"预算记账：3 次工具调用（实际 {end['usage']['tool_calls']}）")

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    check(detail["finished"] is True, "run 已标记 finished")
    check(
        (detail["summary"] or {}).get("plan", {}).get("innovations") is not None,
        "GET /api/runs/{id} 能拿到落盘的 plan（刷新页面不丢产物）",
    )
    check(
        (ROOT / "data" / run_id / "plan.json").exists(),
        "plan.json 已落盘",
    )
    check(
        len(detail["events"]) == len(events),
        f"events.jsonl 与实时事件条数一致（{len(detail['events'])} 条）",
    )
    check(detail["meta"]["paper"]["page_count"] == 6, "meta.json 里记录了论文信息")


async def section_c(check: Checker, client: httpx.AsyncClient) -> None:
    check.section("C. 诚实性机制：编造引文会被打回重做")

    run_id = await upload_paper(client, check, "bad-plan")
    events = await run_recon(client, check, run_id, "bad-plan")

    calls = [call["data"]["tool"] for call in events_of(events, "tool_call")]
    check(
        calls == ["list_pages", "get_page_text", "record_plan", "record_plan"],
        f"模型提交了两次 record_plan（第一次不合格）：{calls}",
    )
    results = events_of(events, "tool_result")
    first_plan_result = [r for r in results if r["data"]["tool"] == "record_plan"][0]
    check(first_plan_result["data"]["is_error"] is True, "第一次 record_plan 被标记为错误")
    check(
        "找不到" in first_plan_result["data"]["summary"],
        f"打回信息说清了原因：{first_plan_result['data']['summary'][:80]}",
    )
    plan = events_of(events, "plan_ready")[0]["data"]
    check(plan["unverified_quotes"] == 0, "修正后的清单引文全部通过核验")
    end = events_of(events, "run_end")[0]["data"]
    check(end["status"] == "ok" and end["stopped_reason"] == "Agent 主动结束", "修正后正常结束")


# ---------------------------------------------------------------------------
# D. 上传护栏
# ---------------------------------------------------------------------------
async def section_d(check: Checker) -> None:
    check.section("D. 上传与接口护栏")
    from app.config import settings
    from app.main import app

    pdf = (ROOT / "tests" / "fixtures" / "synthetic_paper.pdf").read_bytes()
    form = {"provider": json.dumps(provider("mock-model"))}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as client:
        response = await client.post(
            "/api/runs", files={"file": ("notes.txt", b"just text", "text/plain")}, data=form
        )
        check(response.status_code == 400, f"非 PDF 被拒绝（HTTP {response.status_code}）")

        original_mb = settings.max_upload_mb
        settings.max_upload_mb = 1
        try:
            big = b"%PDF-1.4\n" + b"0" * 2_000_000
            response = await client.post(
                "/api/runs", files={"file": ("big.pdf", big, "application/pdf")}, data=form
            )
            check(response.status_code == 413, f"超过体积上限被拒绝（HTTP {response.status_code}）")
        finally:
            settings.max_upload_mb = original_mb

        original_pages = settings.max_pages
        settings.max_pages = 3
        try:
            response = await client.post(
                "/api/runs", files={"file": ("p.pdf", pdf, "application/pdf")}, data=form
            )
            check(response.status_code == 400, f"超过页数上限被拒绝（HTTP {response.status_code}）")
        finally:
            settings.max_pages = original_pages

        response = await client.post(
            "/api/runs", files={"file": ("p.pdf", pdf, "application/pdf")}, data={"provider": "{not json"}
        )
        check(response.status_code == 422, f"provider 配置非法被拒绝（HTTP {response.status_code}）")

        response = await client.post("/api/runs/no-such-run/recon", json={"provider": provider("mock-model")})
        check(response.status_code == 404, f"未知 run 返回 404（HTTP {response.status_code}）")

        response = await client.post("/api/runs", files={"file": ("p.pdf", pdf, "application/pdf")}, data=form)
        run_id = response.json()["run_id"]
        response = await client.get(f"/api/runs/{run_id}")
        check(response.status_code == 200, "刚上传还没跑 recon 时，GET run 也能正常工作")
        check(response.json()["phase"] is None, "阶段尚未开始，phase 为空")


async def section_e(check: Checker, client: httpx.AsyncClient) -> None:
    """回归：**先连事件流、再启动阶段**。

    浏览器就是这么干的（页面先把 EventSource 连上，用户再点按钮）。
    如果后端在阶段启动时换掉事件总线，已经连上的客户端就会永远收不到新事件，
    界面上表现为一直卡在"侦察中…"。
    """
    check.section("E. 先连事件流、再启动阶段（浏览器真实顺序）")

    run_id = await upload_paper(client, check, "mock-model")

    # 先建立连接，并给它一点时间真正连上
    stream = asyncio.create_task(
        collect_sse(client, f"/api/runs/{run_id}/events", stop_on_run_end=True, timeout_s=60)
    )
    await asyncio.sleep(0.6)

    started = await client.post(f"/api/runs/{run_id}/recon", json={"provider": provider("mock-model")})
    check(started.status_code == 200, "阶段 A 启动成功")

    events, _, _ = await stream
    types = [event["type"] for event in events]
    check("run_start" in types, f"连上之后启动的阶段，事件能实时收到（收到 {len(events)} 条：{types[:6]}…）")
    check("plan_ready" in types, "plan_ready 也收到了（界面不会卡在'侦察中'）")
    check(types and types[-1] == "run_end", "以 run_end 收尾")


async def section_f(check: Checker, client: httpx.AsyncClient) -> None:
    """限流（429）回归：退避重试 + 失败也要交付已确认的部分。

    起因（2026-09-15 用户实测）：某中转站限制"1 分钟最多 10 次，含失败次数"，而一次定位
    要几十次调用 → 必然撞限流；原来的代码撞到 429 直接让整个 run failed，用户前面跑出来的
    东西全丢。所以这里钉两件事：
      ① 429 要能自动退避重试并跑完（不是一撞就死）；
      ② 重试用尽仍然失败时，已记录的结论必须照常核验、落盘、交付。
    """
    from app import providers as providers_module
    from app.config import settings

    check.section("F. 网关限流（429）：退避重试 + 失败也交付")

    # --- ① 只拦第一次：退避后应能跑完整条侦察 ---
    providers_module.reset_llm_pacing()
    try:
        run_id = await upload_paper(client, check, "rate-limited-once")
        recon = await client.post(
            f"/api/runs/{run_id}/recon", json={"provider": provider("rate-limited-once")}
        )
        check(recon.status_code == 200, "限流端点上阶段 A 能启动")
        events, _, _ = await collect_sse(client, f"/api/runs/{run_id}/events", timeout_s=120)
        types = [event["type"] for event in events]
        retries = events_of(events, "llm_retry")
        check(bool(retries), f"撞到 429 会发 llm_retry 事件（用户看得见在等）：{types[:5]}…")
        if retries:
            payload = retries[0]["data"]
            check(
                payload.get("delay_seconds", 0) > 0 and payload.get("attempt") == 1,
                f"重试事件带等待秒数与次数（等待 {payload.get('delay_seconds')}s，第 {payload.get('attempt')} 次）",
            )
        check("plan_ready" in types, "退避重试之后侦察照样跑完（不是一撞限流就死）")
        check(events and events[-1]["type"] == "run_end", "以 run_end 收尾")
        check(
            events_of(events, "run_end")[0]["data"]["status"] == "ok",
            f"限流恢复后这一轮状态是 ok（实际 {events_of(events, 'run_end')[0]['data']['status']}）",
        )
        # 节奏学习是**后端进程内**的状态，HTTP 层读不到——这里直接单测那段逻辑
        # （端到端证据是上面那条：等待 6.0s 就是把"1 分钟 10 次"算成了 6s/次）。
        providers_module.reset_llm_pacing()
        providers_module._retry_delay(
            RuntimeError("您已达到总请求数限制：1分钟内最多请求10次，包括失败次数"),
            0,
            "rate_limit",
        )
        check(
            providers_module._effective_llm_interval() >= 5.0,
            f"从网关话术学到限额并自动降速（最小间隔 {providers_module._effective_llm_interval():.1f}s）",
        )
    finally:
        providers_module.reset_llm_pacing()   # 别拖慢后面的验收

    # --- ② 攒够几轮后一直拦：run 失败，但已确认的部分必须交付 ---
    providers_module.reset_llm_pacing()
    saved_retries = settings.llm_max_retries
    try:
        settings.llm_max_retries = 1          # 断言要快：退避 1 次就放弃
        run_id = await upload_paper(client, check, "rate-limited-late-2")
        recon = await client.post(
            f"/api/runs/{run_id}/recon", json={"provider": provider("rate-limited-late-2")}
        )
        check(recon.status_code == 200, "持续限流的端点上阶段 A 能启动")
        events, _, _ = await collect_sse(client, f"/api/runs/{run_id}/events", timeout_s=180)
        end = events_of(events, "run_end")
        check(bool(end), "持续限流最终以 run_end 收尾（不会挂住）")
        if end:
            status = end[0]["data"]["status"]
            reason = str(end[0]["data"].get("stopped_reason", ""))
            check(status == "failed", f"重试用尽后如实标记失败（status={status}）")
            check(
                "限流" in reason or "429" in reason or "RateLimit" in reason,
                f"失败原因说人话、指得出是限流：{reason[:90]}…",
            )
            check(
                end[0]["data"].get("partial") is True,
                "失败轮标记为 partial（前端能区分'跑完了'和'只交付了一部分'）",
            )
        # 失败也要留下可查的现场（清单是否已产出取决于限流来得多早；
        # 「已确认的结论照样交付」由 m2 的 I 段钉——那里是定位阶段，会先攒下几条结论）。
        detail = await client.get(f"/api/runs/{run_id}")
        check(detail.status_code == 200, "失败后仍能取到 run 详情")
        payload = detail.json()
        check(
            isinstance(payload.get("events"), list) and payload["events"],
            f"失败现场的事件历史仍在（{len(payload.get('events') or [])} 条）",
        )
        check(
            "error" in [event["type"] for event in events],
            "失败路径发了 error 事件（用户知道为什么停）",
        )
    finally:
        settings.llm_max_retries = saved_retries
        providers_module.reset_llm_pacing()


async def section_g(check: Checker, client: httpx.AsyncClient) -> None:
    """不同网关风格的限额识别：权威信号优先 + 分类报错（2026-09-15 第二轮）。

    上一轮只覆盖了"网关在错误话术里写了限额"这一种；这一轮补上另外三种真实存在的风格：
      ① 只在**响应头**里声明限额（OpenAI 官方风格，话术里一个字不提）；
      ② **额度用尽**（长得像 429，但重试永远不会成功）；
      ③ **TPM**（token/分钟，按请求降速没用，得按 token 节流）。
    """
    from app import providers as providers_module

    check.section("G. 不同网关风格的限额识别（响应头 / 额度用尽 / TPM）")

    # ---- ① 只在响应头里声明限额：不靠话术也要学会 ----
    pacing_before = (await client.get("/api/health")).json().get("llm_pacing", {})
    headers_run = await upload_paper(client, check, "headers-only-limit")
    events = await run_recon(client, check, headers_run, "headers-only-limit")
    pacing_after = (await client.get("/api/health")).json().get("llm_pacing", {})
    check(
        pacing_after.get("source") == "header",
        f"从响应头学会了端点限额（source={pacing_after.get('source')}）——不依赖任何话术或配置",
    )
    check(
        abs(float(pacing_after.get("min_interval_seconds", 0)) - 3.0) < 0.2,
        f"头里的 `20, 20;w=60` 被换算成 3.0s/次（实际 {pacing_after.get('min_interval_seconds')}s）",
    )
    check(
        "run_end" in [event["type"] for event in events],
        f"只声明在头里的端点上照样跑完（pacing 从 {pacing_before.get('min_interval_seconds')}s 变为 "
        f"{pacing_after.get('min_interval_seconds')}s）",
    )
    smoke = await client.post(
        "/api/provider/smoke-test", json=provider("headers-only-limit")
    )
    diagnosis = str(smoke.json().get("diagnosis", ""))
    check(
        "端点限额已识别" in diagnosis and "3.0s" in diagnosis,
        f"自检把限额与节奏提前报给用户（{diagnosis[:80]}…）",
    )

    # ---- ② 额度用尽：不重试，快速失败，文案说清"重试无用" ----
    quota_run = await upload_paper(client, check, "quota-exhausted")
    started = await client.post(
        f"/api/runs/{quota_run}/recon", json={"provider": provider("quota-exhausted")}
    )
    check(started.status_code == 200, "额度用尽的端点上阶段 A 能启动")
    baseline_events, _, _ = await collect_sse(client, f"/api/runs/{quota_run}/events", timeout_s=120)
    quota_types = [event["type"] for event in baseline_events]
    end = events_of(baseline_events, "run_end")
    check(bool(end) and end[0]["data"]["status"] == "failed", "额度用尽如实标记失败")
    reason = str(end[0]["data"].get("stopped_reason", "")) if end else ""
    check(
        "额度" in reason or "余额" in reason,
        f"文案说清是账号额度问题：{reason[:70]}…",
    )
    check(
        "重试无用" in reason,
        "并明确告诉用户重试没有用（否则用户会一直重试）",
    )
    check(
        "llm_retry" not in quota_types,
        f"额度用尽**不重试**（事件里没有 llm_retry；{quota_types[:6]}…）",
    )
    if end:
        seconds = float(end[0]["data"].get("usage", {}).get("seconds", 0))
        check(seconds < 20, f"没有白等：{seconds:.1f}s 就结束了（若按限流重试会是 60s+）")

    # ---- ③ TPM：按 token 节流 + 给出正确的建议 ----
    tpm_run = await upload_paper(client, check, "tpm-limited")
    started = await client.post(
        f"/api/runs/{tpm_run}/recon", json={"provider": provider("tpm-limited")}
    )
    check(started.status_code == 200, "TPM 端点上阶段 A 能启动")
    tpm_events, _, _ = await collect_sse(client, f"/api/runs/{tpm_run}/events", timeout_s=200)
    tpm_end = events_of(tpm_events, "run_end")
    tpm_reason = str(tpm_end[0]["data"].get("stopped_reason", "")) if tpm_end else ""
    check(bool(tpm_end) and tpm_end[0]["data"]["status"] == "failed", "TPM 端点如实失败")
    check(
        "token" in tpm_reason.lower() or "TPM" in tpm_reason,
        f"文案指出卡的是 token 速率而不是请求次数：{tpm_reason[:80]}…",
    )
    check(
        "拉长请求间隔" in tpm_reason or "减少每次调用的上下文" in tpm_reason,
        "并给出对 TPM 真正有用的建议（而不是让它去调请求间隔）",
    )
    tpm_pacing = (await client.get("/api/health")).json().get("llm_pacing", {})
    check(
        int(tpm_pacing.get("token_limit_per_minute", 0)) == 200000,
        f"从话术里学到 token 限额并启用 token 节流（{tpm_pacing.get('token_limit_per_minute')}/分钟）",
    )

    # ---- ④ 纯函数层：分类 / 头解析 / token 等待 / 持久化 ----
    class _FakeError(Exception):
        def __init__(self, message: str, status: int | None = None) -> None:
            super().__init__(message)
            self.status_code = status

    check(
        providers_module.classify_llm_error(_FakeError("余额不足", 429)) == "quota_exhausted"
        and providers_module.classify_llm_error(_FakeError("payment required", 402)) == "quota_exhausted"
        and providers_module.classify_llm_error(_FakeError("200000 tokens per minute", 429)) == "token_limit"
        and providers_module.classify_llm_error(_FakeError("1分钟内最多请求10次", 429)) == "rate_limit"
        and providers_module.classify_llm_error(_FakeError("unexpected response shape")) == "other",
        "错误分类：额度用尽（含 402）/ TPM / 普通限流 / 其他 各归各类（transient 由 H 段钉）",
    )
    check(
        providers_module._interval_from_limit_header("10, 10;w=60") == 6.0
        and providers_module._interval_from_limit_header("60, 60;w=60") == 1.0
        and providers_module._token_limit_from_message("Rate limit reached for 200000 tokens per minute") == 200000.0,
        "标准响应头与话术都能换算成节奏/token 限额",
    )
    providers_module.reset_llm_pacing()
    providers_module._LLM_TOKEN_WINDOW.append((time.monotonic(), 300))
    check(
        providers_module._token_wait_seconds(800, 1000, time.monotonic()) > 0
        and providers_module._token_wait_seconds(100, 1000, time.monotonic()) == 0,
        "token 节流：窗口内快超限就等、没超就不等（TPM 靠它而不是靠拉长请求间隔）",
    )
    providers_module.reset_llm_pacing()
    pace_file = ROOT / "data" / "llm-pace.json"
    if pace_file.exists():
        text = pace_file.read_text(encoding="utf-8")
        check(
            "sk-" not in text and "api_key" not in text,
            "学到的节奏落盘了，且文件里**不含任何密钥**（只存节奏）",
        )
    else:
        check(True, "学到的节奏尚未落盘（本轮没有需要持久化的端点）")


async def section_h(check: Checker, client: httpx.AsyncClient) -> None:
    """瞬时故障（连接错误 / 网关 5xx）：自动重试；中途断线则如实失败并保留成果。

    起因（2026-09-15 用户实测）：跑到第 10 轮（16 次工具调用 / 5 条创新点）时
    `InternalServerError: Connection error.` → 旧代码归到「其他」，**不重试**，整轮作废。
    网络抖动本来是最该重试的一类错误。
    """
    from app import providers as providers_module

    check.section("H. 瞬时故障（连接错误 / 网关 5xx）：重试 + 中途断线不静默成功")

    # ---- ① 第一次就 500 "Connection error."：应当自动重试并跑完 ----
    run_id = await upload_paper(client, check, "flaky-once")
    events = await run_recon(client, check, run_id, "flaky-once")
    types = [event["type"] for event in events]
    retries = events_of(events, "llm_retry")
    check(bool(retries), f"瞬时故障会重试并留痕（{types[:6]}…）")
    if retries:
        payload = retries[0]["data"]
        check(
            payload.get("reason") == "transient",
            f"事件里标明是瞬时故障而不是限流（reason={payload.get('reason')}）",
        )
        check(
            "Connection" in str(payload.get("detail", "")) or "连接" in str(payload.get("detail", "")),
            f"细节里带上原始错误，便于排障：{str(payload.get('detail'))[:60]}…",
        )
    check("plan_ready" in types, "重试之后侦察照样跑完（不会因为一次网络抖动白跑）")
    end = events_of(events, "run_end")
    check(
        bool(end) and end[0]["data"]["status"] == "ok",
        f"这一轮状态是 ok（实际 {end[0]['data']['status'] if end else '无 run_end'}）",
    )

    # ---- ② 流到一半掐断：必须失败，不能把半截响应当成完整回答 ----
    mid_run = await upload_paper(client, check, "flaky-midstream")
    mid_events = await run_recon(client, check, mid_run, "flaky-midstream")
    mid_types = [event["type"] for event in mid_events]
    mid_end = events_of(mid_events, "run_end")
    check(bool(mid_end), "中途断线也会正常收尾（不会挂住）")
    if mid_end:
        status = mid_end[0]["data"]["status"]
        reason = str(mid_end[0]["data"].get("stopped_reason", ""))
        check(status == "failed", f"中途断线如实失败（status={status}）")
        check(
            "连接" in reason or "Connection" in reason or "中途" in reason,
            f"失败原因说清是连接问题：{reason[:70]}…",
        )
        check(
            "重跑一次" in reason or "照常交付" in reason,
            "并说清处置办法（重跑 / 已确认的部分照常交付）",
        )
        check("plan_ready" not in mid_types, "没有把半截响应当成完整回答继续往下走")
        check(
            mid_end[0]["data"].get("partial") is True,
            "标 partial：已确认的成果照常交付（半截内容不会被当成结论）",
        )

    # ---- ③ 纯函数层：分类与退避 ----
    class _FakeError(Exception):
        def __init__(self, message: str, status: int | None = None) -> None:
            super().__init__(message)
            self.status_code = status

    check(
        providers_module.classify_llm_error(_FakeError("OpenAIException - Connection error.", 500)) == "transient"
        and providers_module.classify_llm_error(_FakeError("Connection reset by peer")) == "transient"
        and providers_module.classify_llm_error(_FakeError("502 Bad Gateway", 502)) == "transient"
        and providers_module.classify_llm_error(_FakeError("Server disconnected without sending a response")) == "transient",
        "连接错误 / 断连 / 5xx 都归入 transient（这类最该重试）",
    )
    check(
        providers_module.classify_llm_error(_FakeError("Request timed out.")) == "other",
        "超时**刻意不算** transient：那是「端点太慢」，重试只会在同一时限上再等一遍",
    )
    delays = [providers_module._retry_delay(_FakeError("Connection error.", 500), i, "transient") for i in range(4)]
    check(
        delays == [5.0, 10.0, 20.0, 40.0],
        f"瞬时故障的退避比限流更快起跳（{delays}）——抖动通常几秒内恢复",
    )
    providers_module.reset_llm_pacing()


async def main() -> int:
    check = Checker("M1 验收")
    mock = start_service("devtools.mock_provider:app", MOCK_PORT)
    app_proc = start_service("app.main:app", APP_PORT)
    try:
        await wait_http(f"http://127.0.0.1:{MOCK_PORT}/health")
        await wait_http(f"{APP}/api/health")
        print(f"mock provider : {MOCK_PORT}\nPaperLens API : {APP}", flush=True)

        await section_a(check)

        async with httpx.AsyncClient(base_url=APP, timeout=180) as client:
            await section_b(check, client)
            await section_c(check, client)
            await section_e(check, client)
            await section_g(check, client)
            await section_f(check, client)
            await section_h(check, client)

        await section_d(check)
        return check.finish()
    finally:
        stop_services(mock, app_proc)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
