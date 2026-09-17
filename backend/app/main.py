"""PaperLens FastAPI 后端。

端点：
    GET  /api/health                       健康检查
    POST /api/provider/smoke-test          §4 启动自检
    POST /api/runs                         上传论文 PDF + provider，建立一次 run
    POST /api/runs/{id}/recon              阶段 A（侦察）：读论文 → 产出创新点清单
    POST /api/analyze                      M0 链路自检（无论文的双工具 Agent）
    GET  /api/runs/{id}/events             SSE 事件流（支持 Last-Event-ID 断线重连）
    GET  /api/runs/{id}                    事件历史 + 产物（刷新页面 / 回放）
    POST /api/runs/{id}/cancel             取消运行

安全约定（§10）：api_key 只在请求体里出现，不写日志、不落库、不进错误堆栈。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from . import store
from .agent.loop import run_agent
from .agent.tools.base import Budget, Tool
from .agent.tools.builtin import FINISH, M0_TOOLS, build_m0_system_prompt, build_m0_user_prompt
from .agent.tools.findings import RECORD_FINDING, RECORD_PLAN
from .agent.tools.paper_tools import PAPER_TOOLS
from .agent.tools.repo_tools import REPO_TOOLS
from .config import settings
from .events import RunBus, load_events, to_sse
from .chat import (
    append_turn as append_chat_turn,
)
from .chat import build_chat_prompt, build_context_block, load_transcript, verify_citations
from .paper import PaperDocument
from .plan import PlanItemIn, PlanItemPatch
from .plan import add_item as plan_add_item
from .plan import delete_item as plan_delete_item
from .plan import load_plan as load_plan_from_disk
from .plan import update_item as plan_update_item
from .prompts import (
    CHAT_SYSTEM_PROMPT,
    LOCATE_SYSTEM_PROMPT,
    RECON_SYSTEM_PROMPT,
    build_chat_user_prompt,
    build_locate_user_prompt,
    build_recon_user_prompt,
)
from .providers import ProviderConfig, SmokeResult, smoke_test
from .repo_source import RepoError, RepoSource, clone_repo, text_lines, validate_repo_url
from .verify import summarize as verify_summarize
from .verify import verify_artifact

app = FastAPI(title="PaperLens API", version="0.1.0-m1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


class _Run:
    __slots__ = ("run_id", "bus", "task", "summary", "created_at", "phase")

    def __init__(self, run_id: str, bus: RunBus) -> None:
        self.run_id = run_id
        self.bus = bus
        self.task: asyncio.Task | None = None
        self.summary: dict[str, Any] | None = None
        self.created_at = time.time()
        self.phase: str | None = None


RUNS: dict[str, _Run] = {}


def _registry(run_id: str) -> _Run:
    """拿到 run 的登记项；不存在就按磁盘上的痕迹恢复（进程重启 / 刷新页面之后依然可回放）。"""
    run = RUNS.get(run_id)
    if run is None:
        if not store.meta_path(run_id).exists():
            raise HTTPException(status_code=404, detail=f"未知的 run_id: {run_id}")
        run = _Run(run_id, RunBus(run_id, store.events_path(run_id)))
        RUNS[run_id] = run
    return run


async def _require_idle(run: _Run) -> None:
    if run.task and not run.task.done():
        raise HTTPException(status_code=409, detail="这个 run 上已经有一个阶段在运行")


def _budget() -> Budget:
    return Budget(
        max_tool_calls=settings.max_tool_calls,
        max_input_tokens=settings.max_input_tokens,
        wall_clock_seconds=settings.wall_clock_seconds,
        started=time.monotonic(),
    )


@app.get("/api/health")
async def health() -> dict[str, Any]:
    from .providers import llm_pacing_status

    return {
        "ok": True,
        "stage": "M2",
        # 端点的生效节奏（配置/响应头/话术/学到的，谁生效看 source）——用户排障时最想知道的事
        "llm_pacing": llm_pacing_status(),
        "limits": {
            "max_tool_calls": settings.max_tool_calls,
            "max_input_tokens": settings.max_input_tokens,
            "wall_clock_seconds": settings.wall_clock_seconds,
            "max_upload_mb": settings.max_upload_mb,
            "max_pages": settings.max_pages,
        },
    }


@app.post("/api/provider/smoke-test", response_model=SmokeResult)
async def provider_smoke_test(cfg: ProviderConfig) -> SmokeResult:
    """§4 启动自检：不支持可靠工具调用的模型必须在这里就被拦住。"""
    return await smoke_test(cfg)


# ---------------------------------------------------------------------------
# 上传论文 + 建立 run
# ---------------------------------------------------------------------------
@app.post("/api/runs")
async def create_run(
    file: UploadFile = File(...),
    provider: str = Form(...),
) -> dict[str, Any]:
    try:
        cfg = ProviderConfig.model_validate_json(provider)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"provider 配置不合法：{exc}") from exc

    # 本地单用户场景下把文件读进内存是够用的（上限 50MB）；
    # 要支持公网多用户时再改成流式落盘。
    data = await file.read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"文件超过 {settings.max_upload_mb}MB 上限")
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="这不是一个 PDF 文件")

    run_id = store.new_run_id()
    directory = store.run_dir(run_id)
    path = store.paper_path(run_id)
    path.write_bytes(data)

    doc: PaperDocument | None = None
    try:
        doc = PaperDocument(path, cache_dir=directory / "cache")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"PDF 打不开：{exc}") from exc

    try:
        if doc.page_count > settings.max_pages:
            raise HTTPException(
                status_code=400, detail=f"论文共 {doc.page_count} 页，超过 {settings.max_pages} 页上限"
            )
        title_guess = doc.title_guess
        page_count = doc.page_count
    finally:
        doc.close()

    paper_meta = {
        "title_guess": title_guess,
        "page_count": page_count,
        "sha256": store.file_sha256(path),
        "bytes": len(data),
        "filename": file.filename,
    }
    store.write_meta(run_id, paper=paper_meta, provider_label=cfg.safe_label())
    bus = RunBus(run_id, store.events_path(run_id))
    RUNS[run_id] = _Run(run_id, bus)

    await bus.emit("paper_ready", paper=paper_meta, provider=cfg.safe_label())
    return {
        "run_id": run_id,
        "paper": paper_meta,
        "provider": cfg.safe_label(),
        "events_url": f"/api/runs/{run_id}/events",
    }


# ---------------------------------------------------------------------------
# 阶段 A：侦察
# ---------------------------------------------------------------------------
class ReconRequest(BaseModel):
    provider: ProviderConfig
    extra_instructions: str | None = None


@app.post("/api/runs/{run_id}/recon")
async def start_recon(run_id: str, req: ReconRequest) -> dict[str, Any]:
    run = _registry(run_id)
    meta = store.read_meta(run_id)
    paper_meta = meta.get("paper")
    if not paper_meta:
        raise HTTPException(status_code=409, detail="这个 run 还没有上传论文")

    await _require_idle(run)

    # 关键：**复用同一个 bus**，不要在阶段切换时新建。
    # 前端是"打开页面就连上事件流、再点按钮启动阶段"，
    # 如果这里换成新 bus，已经连上的客户端就还挂在旧 bus 上，永远收不到事件
    # —— 界面上表现为一直卡在"侦察中…"。
    bus = run.bus
    run.phase = "recon"
    store.write_meta(run_id, phase="recon", model=req.provider.model)

    directory = store.run_dir(run_id)
    tools: list[Tool] = [*PAPER_TOOLS, RECORD_PLAN]

    async def _job() -> None:
        doc = PaperDocument(store.paper_path(run_id), cache_dir=directory / "cache")
        try:
            run.summary = await run_agent(
                cfg=req.provider,
                tools=tools,
                system_prompt=RECON_SYSTEM_PROMPT,
                user_prompt=build_recon_user_prompt(
                    title_guess=paper_meta.get("title_guess", ""),
                    page_count=paper_meta.get("page_count", 0),
                ),
                bus=bus,
                budget=_budget(),
                run_dir_path=directory,
                paper=doc,
                meta={"phase": "recon", "provider_label": req.provider.safe_label()},
            )
        finally:
            doc.close()
        store.write_json(directory / "plan.json", run.summary or {})

    run.task = asyncio.create_task(_job())
    return {"run_id": run_id, "phase": "recon", "started": True}


# ---------------------------------------------------------------------------
# 阶段 C：用户自己指定要看懂什么（在原文里划选 → 加为定位目标）
#
# 清单是可变状态，直接落在 plan.json 的同一个 plan 对象上，
# 所以定位阶段天然会把这些目标一起处理，不需要另开通路。
# ---------------------------------------------------------------------------
def _plan_or_404(run_id: str) -> dict[str, Any]:
    plan = load_plan_from_disk(run_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="这个 run 还没有清单（先跑侦察，或直接加一个自定义目标）")
    return plan


@app.get("/api/runs/{run_id}/plan")
async def get_plan(run_id: str) -> dict[str, Any]:
    _registry(run_id)
    plan = load_plan_from_disk(run_id)
    return {"run_id": run_id, "plan": plan, "user_items": sum(1 for i in (plan or {}).get("innovations", []) if i.get("source") == "user")}


@app.post("/api/runs/{run_id}/plan/items")
async def add_plan_item(run_id: str, payload: PlanItemIn) -> dict[str, Any]:
    """把"我想看懂这一段"变成一个定位目标。没跑过侦察也能用。"""
    run = _registry(run_id)
    if run.task is not None and not run.task.done():
        raise HTTPException(status_code=409, detail="有阶段正在运行，等它结束再改清单")
    if not payload.name and not payload.quote:
        raise HTTPException(status_code=422, detail="至少给一个名称或一段原文，否则不知道该找什么")
    plan = plan_add_item(run_id, payload)
    added = plan["innovations"][-1]
    bus = run.bus
    await bus.emit("plan_updated", plan=plan, action="add", item_id=added["id"])
    return {"run_id": run_id, "plan": plan, "added": added}


@app.patch("/api/runs/{run_id}/plan/items/{item_id}")
async def patch_plan_item(run_id: str, item_id: str, patch: PlanItemPatch) -> dict[str, Any]:
    run = _registry(run_id)
    # 和 add 一样的守卫：阶段跑着的时候清单不能改——targets 已经快照，
    # 改了也不会生效，只会让产物和清单对不上，用户还以为改动被采纳了。
    await _require_idle(run)
    try:
        plan = plan_update_item(run_id, item_id, patch)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"清单里没有 {item_id}") from exc
    await run.bus.emit("plan_updated", plan=plan, action="update", item_id=item_id)
    return {"run_id": run_id, "plan": plan}


@app.delete("/api/runs/{run_id}/plan/items/{item_id}")
async def delete_plan_item(run_id: str, item_id: str) -> dict[str, Any]:
    run = _registry(run_id)
    await _require_idle(run)  # 同 patch：阶段运行中不许改清单
    try:
        plan = plan_delete_item(run_id, item_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"清单里没有 {item_id}") from exc
    await run.bus.emit("plan_updated", plan=plan, action="delete", item_id=item_id)
    return {"run_id": run_id, "plan": plan}


# ---------------------------------------------------------------------------
# 阶段 B：定位（克隆仓库 → Agent 探索 → 结构化结论 → 机械核验）
# ---------------------------------------------------------------------------
class LocateRequest(BaseModel):
    provider: ProviderConfig
    repo_url: str
    selected_ids: list[str] | None = None


def _load_plan(run_id: str) -> dict[str, Any] | None:
    summary = store.read_json(store.run_dir(run_id) / "plan.json") or {}
    return summary.get("plan")


def _build_artifact(
    run_id: str,
    findings: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    repo_info: dict[str, Any],
    cfg: ProviderConfig,
    summary: dict[str, Any],
) -> dict[str, Any]:
    """把 Agent 的结论组装成交付物。结构见 docs/v0-spec.md §6。"""
    meta = store.read_meta(run_id)
    by_id = {finding["id"]: finding for finding in findings}
    missing = [target["id"] for target in targets if target["id"] not in by_id]
    ordered = [by_id[target["id"]] for target in targets if target["id"] in by_id]
    return {
        "schema_version": "0.2",
        "run": {
            "run_id": run_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "paper": meta.get("paper", {}),
            "repo": repo_info,
            "provider": {"protocol": cfg.protocol, "model": cfg.model},
            "prompt_version": settings.prompt_version,
            "budget_used": summary.get("usage", {}),
        },
        "innovations": ordered,
        "missing_ids": missing,
        "not_found": [
            {
                "id": finding["id"],
                "name": finding.get("name"),
                "reason": finding.get("not_found_reason"),
                "searched": finding.get("searched", []),
            }
            for finding in ordered
            if finding.get("status") == "not_found"
        ],
        "coverage": {
            "files_total": repo_info.get("files_total"),
            "stopped_reason": summary.get("stopped_reason"),
            "coverage_note": summary.get("coverage_note"),
        },
    }


@app.post("/api/runs/{run_id}/locate")
async def start_locate(run_id: str, req: LocateRequest) -> dict[str, Any]:
    run = _registry(run_id)
    await _require_idle(run)

    plan = _load_plan(run_id)
    if not plan:
        raise HTTPException(
            status_code=409,
            detail="还没有定位目标：先运行侦察让 Agent 梳理，或者在论文里划选一段自己指定",
        )

    wanted = set(req.selected_ids or [])
    targets = [item for item in plan["innovations"] if not wanted or item["id"] in wanted]
    if not targets:
        raise HTTPException(
            status_code=422,
            detail=f"selected_ids 和清单里的 id 对不上（清单里有 {[i['id'] for i in plan['innovations']]}）",
        )

    # 先校验仓库地址：地址本身有问题就别启动任务了（省得用户等半天才看到失败）。
    # 返回值是守卫的判定提示（例如"本机解析到代理占位地址，local 模式已放行"）——
    # 必须留痕并带给前端，否则用户不知道守卫这次为什么没拦。
    # 丢进线程池：hosted 模式下这一步会做公网 DoH 核验（有网络超时），
    # 直接在事件循环里跑会把整个后端卡住最多几秒。
    try:
        repo_notes = await asyncio.to_thread(validate_repo_url, req.repo_url)
    except RepoError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    bus = run.bus  # 同上：阶段切换不换总线
    run.phase = "locate"
    store.write_meta(
        run_id,
        phase="locate",
        repo_url=req.repo_url,
        selected=[t["id"] for t in targets],
        repo_notes=repo_notes,
    )

    directory = store.run_dir(run_id)
    budget = _budget()

    async def _job() -> None:
        try:
            await _locate_body()
        except Exception as exc:  # noqa: BLE001 —— 任何意外都要变成用户看得见的事件，不能静默死掉
            await bus.emit("error", kind=type(exc).__name__, message=str(exc)[:500])
            await bus.emit(
                "run_end",
                status="failed",
                stopped_reason=f"{type(exc).__name__}",
                usage=budget.snapshot(),
            )

    async def _locate_body() -> None:
        await bus.emit("repo_cloning", url=req.repo_url, commit_sha=None)

        # 克隆是同步阻塞调用，必须丢进线程池：否则慢克隆（最长 300s）会把整个
        # 事件循环冻住——其他请求、SSE 心跳全部停摆。进度经线程安全队列转发，
        # 由独立任务变成 clone_progress 事件（时间线上的实时进度条）。
        progress_queue: asyncio.Queue[str] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _on_progress(line: str) -> None:
            loop.call_soon_threadsafe(progress_queue.put_nowait, line)

        async def _drain_clone_progress() -> None:
            while True:
                line = await progress_queue.get()
                await bus.emit("clone_progress", text=line)

        drainer = asyncio.create_task(_drain_clone_progress())
        try:
            # overwrite=True：定位必须可重入。同一个 run 第二次点「开始定位」
            # （换勾选、划选补目标、失败重试）是正常操作，repo/ 是派生数据，
            # 覆盖重克隆即可——否则第二次永远撞「目标目录已存在」。
            info = await asyncio.to_thread(
                clone_repo,
                req.repo_url,
                directory / "repo",
                overwrite=True,
                on_progress=_on_progress,
            )
        except RepoError as exc:
            await bus.emit("error", kind="RepoError", message=str(exc))
            await bus.emit(
                "run_end",
                status="failed",
                stopped_reason=f"仓库克隆失败：{exc}",
                usage=budget.snapshot(),
            )
            return
        finally:
            drainer.cancel()
            try:
                await drainer
            except asyncio.CancelledError:
                pass

        repo_info = info.to_dict()
        await bus.emit("repo_ready", repo=repo_info)
        repo = RepoSource(info.root, info.commit_sha)

        tools = [*REPO_TOOLS, RECORD_FINDING, FINISH]

        async def _finalize(result: dict[str, Any]) -> dict[str, Any]:
            """趁 run_end 之前把产物核验完：前端要能在事件流里看到核验率。"""
            findings = list(result.get("findings") or [])
            artifact = _build_artifact(run_id, findings, targets, repo_info, req.provider, result)
            verification = verify_artifact(repo, artifact)
            artifact["verification"] = verification
            store.write_json(directory / "artifact.json", artifact)
            summary_payload = verify_summarize(artifact, verification)
            await bus.emit(
                "verification_done",
                summary=summary_payload,
                missing_ids=artifact["missing_ids"],
                artifact_path=str(directory / "artifact.json"),
            )
            return {
                "verification": verification,
                "missing_ids": artifact["missing_ids"],
                "artifact_path": str(directory / "artifact.json"),
            }

        run.summary = await run_agent(
            cfg=req.provider,
            tools=tools,
            system_prompt=LOCATE_SYSTEM_PROMPT,
            user_prompt=build_locate_user_prompt(targets, repo_info),
            bus=bus,
            budget=budget,
            run_dir_path=directory,
            repo=repo,
            # 工具要知道"这次要定位哪几条创新点"，否则 record_finding 无从校验 innovation_id
            initial_state={"targets": {item["id"]: item for item in targets}},
            finalize=_finalize,
            meta={
                "phase": "locate",
                "provider_label": req.provider.safe_label(),
                "commit_sha": info.commit_sha,
                "targets": [t["id"] for t in targets],
            },
        )

    run.task = asyncio.create_task(_job())
    return {
        "run_id": run_id,
        "phase": "locate",
        "started": True,
        "targets": [t["id"] for t in targets],
    }


# ---------------------------------------------------------------------------
# 阶段 D：追问对话
#
# 允许它重新查代码与论文（这是用户明确要的），但每条消息有独立的预算，
# 而且回答里出现的每个"文件:行号"都会被机械核对——追问不该变成新的幻觉温床。
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    provider: ProviderConfig
    message: str = Field(min_length=1, max_length=2000)
    context_ids: list[str] = Field(default_factory=list)


def _artifact(run_id: str) -> dict[str, Any] | None:
    return store.read_json(store.run_dir(run_id) / "artifact.json")


def _chat_tools(run_id: str) -> tuple[list[Tool], PaperDocument | None, RepoSource | None]:
    """能查什么就给什么：有论文给论文工具，克隆过仓库给仓库工具。"""
    tools: list[Tool] = []
    directory = store.run_dir(run_id)
    paper: PaperDocument | None = None
    repo: RepoSource | None = None

    pdf = store.paper_path(run_id)
    if pdf.exists():
        paper = PaperDocument(pdf, cache_dir=directory / "cache")
        tools.extend(PAPER_TOOLS)

    repo_dir = directory / "repo"
    artifact = _artifact(run_id) or {}
    commit_sha = (artifact.get("run") or {}).get("repo", {}).get("commit_sha")
    if (repo_dir / ".git").exists() and commit_sha:
        repo = RepoSource(repo_dir, commit_sha)
        tools.extend(REPO_TOOLS)

    tools.append(FINISH)
    return tools, paper, repo


@app.get("/api/runs/{run_id}/chat")
async def get_chat(run_id: str) -> dict[str, Any]:
    _registry(run_id)
    return {"run_id": run_id, "turns": load_transcript(run_id)}


@app.post("/api/runs/{run_id}/chat")
async def post_chat(run_id: str, req: ChatRequest) -> dict[str, Any]:
    run = _registry(run_id)
    await _require_idle(run)

    question = req.message.strip()
    if not question:
        raise HTTPException(status_code=422, detail="消息不能为空")

    directory = store.run_dir(run_id)
    tools, paper, repo = _chat_tools(run_id)
    artifact = _artifact(run_id)
    history = load_transcript(run_id)
    user_prompt = build_chat_user_prompt(
        build_context_block(artifact, req.context_ids),
        build_chat_prompt(history, question, req.context_ids),
    )

    append_chat_turn(run_id, "user", question, context_ids=req.context_ids)
    await run.bus.emit("chat_user", message=question, context_ids=req.context_ids)

    budget = Budget(
        max_tool_calls=settings.chat_max_tool_calls,
        max_input_tokens=settings.chat_max_input_tokens,
        wall_clock_seconds=settings.chat_wall_clock_seconds,
        started=time.monotonic(),
    )
    bus = run.bus
    run.phase = "chat"

    async def _finalize(result: dict[str, Any]) -> dict[str, Any]:
        reply = (result.get("final_text") or "").strip()
        if not reply:
            reply = f"（这次没能在预算内给出回答。停止原因：{result.get('stopped_reason')}）"
        citations = verify_citations(repo, reply)
        tools_used = result.get("tools_used") or []
        append_chat_turn(
            run_id,
            "assistant",
            reply,
            tools_used=tools_used,
            citations=citations,
            stopped_reason=result.get("stopped_reason"),
        )
        await bus.emit(
            "chat_reply",
            message=reply,
            tools_used=tools_used,
            citations=citations,
            unverified=sum(1 for item in citations if not item["verified"]),
            usage=result.get("usage"),
            stopped_reason=result.get("stopped_reason"),
        )
        return {"chat_reply": reply}

    async def _job() -> None:
        try:
            await run_agent(
                cfg=req.provider,
                tools=tools,
                system_prompt=CHAT_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                bus=bus,
                budget=budget,
                run_dir_path=directory,
                paper=paper,
                repo=repo,
                meta={"phase": "chat", "provider_label": req.provider.safe_label()},
                finalize=_finalize,
                max_turns=settings.chat_max_tool_calls + 4,
            )
        finally:
            if paper is not None:
                paper.close()

    run.task = asyncio.create_task(_job())
    return {"run_id": run_id, "started": True, "context_ids": req.context_ids}


# ---------------------------------------------------------------------------
# M3：给前端"点开看原文"的两个端点
#
# 设计要点：代码一律从 **git 对象**里读，和 verify.py 核验时读的是同一份内容。
# 如果这里改成读工作区文件，就会出现"前端看到的"和"被核验的"不是同一份东西，
# 那核验就白做了。
# ---------------------------------------------------------------------------
MAX_VIEW_LINES = 2000  # 阅读器要能一次看到整个文件（超长才截断）


def _repo_for_run(run_id: str) -> RepoSource:
    directory = store.run_dir(run_id)
    repo_dir = directory / "repo"
    if not (repo_dir / ".git").exists():
        raise HTTPException(status_code=409, detail="这个 run 还没有克隆仓库（先跑阶段 B 定位）")
    artifact = store.read_json(directory / "artifact.json") or {}
    commit_sha = (artifact.get("run") or {}).get("repo", {}).get("commit_sha")
    if not commit_sha:
        raise HTTPException(status_code=409, detail="产物里没有记录 commit，无法定位代码版本")
    return RepoSource(repo_dir, commit_sha)


def _source_url(repo_url: str | None, commit_sha: str, path: str, start: int, end: int) -> str | None:
    """给前端一个"去托管站看这一行"的链接。本地路径没有链接。"""
    if not repo_url or not repo_url.startswith("https://"):
        return None
    repo_url = repo_url.rstrip("/").removesuffix(".git")
    if "gitlab" in repo_url:
        return f"{repo_url}/-/blob/{commit_sha}/{path}#L{start}-{end}"
    return f"{repo_url}/blob/{commit_sha}/{path}#L{start}-{end}"


@app.get("/api/runs/{run_id}/file")
async def run_file(
    run_id: str,
    path: str,
    start: int | None = None,
    end: int | None = None,
    focus_start: int | None = None,
    focus_end: int | None = None,
) -> dict[str, Any]:
    """读某个 commit 上的一段代码，带行号。前端"点开引用"就是打这个接口。"""
    _registry(run_id)
    repo = _repo_for_run(run_id)

    try:
        repo.resolve(path)  # 路径穿越检查（真正的读取走 git 对象，不碰工作区）
    except RepoError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        content = repo.content_at_commit(path)
    except RepoError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    lines = text_lines(content)
    total = len(lines)

    first = max(1, start or 1)
    requested_last = min(end or total, total)
    last = min(requested_last, first + MAX_VIEW_LINES - 1)
    truncated = last < requested_last  # 只有真的砍掉了内容才算截断
    if first > last:
        raise HTTPException(status_code=422, detail=f"行区间非法：start={first} > end={last}（文件共 {total} 行）")

    artifact = store.read_json(store.run_dir(run_id) / "artifact.json") or {}
    repo_url = (artifact.get("run") or {}).get("repo", {}).get("url")

    return {
        "path": path,
        "commit_sha": repo.commit_sha,
        "line_start": first,
        "line_end": last,
        "total_lines": total,
        "truncated": truncated,
        "lines": [{"n": number, "text": text} for number, text in enumerate(lines[first - 1 : last], start=first)],
        "focus": {"start": focus_start or first, "end": focus_end or last},
        "source_url": _source_url(repo_url, repo.commit_sha, path, focus_start or first, focus_end or last),
    }


@app.get("/api/runs/{run_id}/pdf")
async def run_pdf(run_id: str) -> FileResponse:
    """把上传的 PDF 原样交给浏览器。

    为什么不自己渲染：浏览器自带的 PDF 阅读器本来就有滚动、缩放、翻页、搜索，
    而且是**真实排版**——公式、图、表格都在。我们只负责把文件给它。
    （代价：PDF 阅读器里的文字选不中给外层用，所以"划选加目标"要走文本视图。）
    """
    _registry(run_id)
    pdf = store.paper_path(run_id)
    if not pdf.exists():
        raise HTTPException(status_code=409, detail="这个 run 还没有论文")
    return FileResponse(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="paper-{run_id}.pdf"'},
    )


@app.get("/api/runs/{run_id}/paper/page/{page}")
async def run_paper_page(run_id: str, page: int, quote: str = "") -> dict[str, Any]:
    """读论文某一页的原文。左栏点"第 N 页"时用。

    带 `?quote=` 时顺带给出**引文在这一页上的高亮矩形**（PDF 点坐标）与页面尺寸，
    前端"原版页面"视图据此叠高亮框——因为浏览器内置 PDF 阅读器不允许外部脚本碰它的 DOM，
    所以高亮必须由我们自己画（2026-09-16 用户实测 `#search=` 在 Chrome 上不生效）。
    """
    _registry(run_id)
    pdf = store.paper_path(run_id)
    if not pdf.exists():
        raise HTTPException(status_code=409, detail="这个 run 还没有论文")

    directory = store.run_dir(run_id)
    doc = PaperDocument(pdf, cache_dir=directory / "cache")
    try:
        if not 1 <= page <= doc.page_count:
            raise HTTPException(status_code=422, detail=f"页码 {page} 超出范围（共 {doc.page_count} 页）")
        width, height = doc.page_box(page)
        rects, coverage = doc.quote_rects(page, quote) if quote.strip() else ([], 0.0)
        return {
            "page": page,
            "page_count": doc.page_count,
            "title_guess": doc.title_guess,
            "text": doc.page_text(page),
            "page_width": width,
            "page_height": height,
            # [[x0,y0,x1,y1], …]，PDF 点；找不到就是空数组（前端如实说明，不画假框）
            "highlight_rects": [[round(v, 2) for v in rect] for rect in rects],
            # 高亮**覆盖率**（匹配到的词 / 引文总词数）：<1 说明引文里混着公式/符号等
            # PDF 文本层匹配不到的部分，前端据此提示"切原文文本看完整引文"
            "highlight_coverage": coverage if quote.strip() else None,
        }
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# M0 链路自检路径（无论文，两个工具跑通链路）
# ---------------------------------------------------------------------------
class AnalyzeRequest(BaseModel):
    provider: ProviderConfig
    task: str | None = None


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest) -> dict[str, Any]:
    run_id = store.new_run_id()
    directory = store.run_dir(run_id)
    bus = RunBus(run_id, store.events_path(run_id))
    run = _Run(run_id, bus)
    run.phase = "m0"
    RUNS[run_id] = run
    store.write_meta(run_id, phase="m0", provider_label=req.provider.safe_label())

    async def _job() -> None:
        run.summary = await run_agent(
            cfg=req.provider,
            tools=M0_TOOLS,
            system_prompt=build_m0_system_prompt(),
            user_prompt=req.task or build_m0_user_prompt(),
            bus=bus,
            budget=_budget(),
            run_dir_path=directory,
            meta={"phase": "m0", "provider_label": req.provider.safe_label()},
        )
        store.write_json(directory / "analysis.json", run.summary)

    run.task = asyncio.create_task(_job())
    return {"run_id": run_id, "events_url": f"/api/runs/{run_id}/events", "provider": req.provider.safe_label()}


# ---------------------------------------------------------------------------
# 事件流与结果
# ---------------------------------------------------------------------------
@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request) -> EventSourceResponse:
    run = _registry(run_id)

    last_id = 0
    for raw in (request.headers.get("last-event-id"), request.query_params.get("from_id")):
        if raw and str(raw).isdigit():
            last_id = int(raw)

    async def publisher():
        async for event in run.bus.subscribe(last_id):
            if await request.is_disconnected():
                break
            yield to_sse(event)
            # 已经结束的 run：把历史（含 run_end）送完就收，别让刷新页面的客户端一直挂着。
            # task is None 的情形是「后端重启后从磁盘恢复的 run」：内存里没有任务，
            # 但历史里有 run_end 就说明它已经结束，同样要收尾——否则回放完就永远挂着。
            if event["type"] == "run_end" and (run.task is None or run.task.done()):
                break

    return EventSourceResponse(publisher(), ping=15)


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: str) -> dict[str, Any]:
    run = _registry(run_id)
    finished = run.task is not None and run.task.done()
    directory = store.run_dir(run_id)
    # 交付物优先：阶段 B 的 artifact.json > 阶段 A 的 plan.json > 内存里的 summary
    artifact = (
        store.read_json(directory / "artifact.json")
        or store.read_json(directory / "plan.json")
        or run.summary
    )
    return {
        "run_id": run_id,
        "phase": run.phase,
        "finished": finished,
        "cancelled": bool(run.task and run.task.cancelled()),
        "summary": run.summary,
        "artifact": artifact,
        "meta": store.read_meta(run_id),
        "events": run.bus.history or load_events(store.events_path(run_id)),
    }


@app.get("/api/runs/{run_id}/paper/page/{page}/image")
async def run_paper_page_image(run_id: str, page: int, dpi: int = 150) -> Response:
    """把论文某一页渲染成 PNG，给"原版页面（带高亮框）"用。

    为什么不是直接内嵌原 PDF：内置阅读器不让脚本注入高亮（见上面那个端点的说明）。
    渲染图 + 我们自己叠的高亮框 → 任何浏览器表现一致，公式与图也照旧是原样渲染出来的。
    """
    _registry(run_id)
    pdf = store.paper_path(run_id)
    if not pdf.exists():
        raise HTTPException(status_code=409, detail="这个 run 还没有论文")
    directory = store.run_dir(run_id)
    doc = PaperDocument(pdf, cache_dir=directory / "cache")
    try:
        data, media_type = doc.page_image(page, dpi=max(72, min(dpi, 300)))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.post("/api/runs/{run_id}/cancel")
async def run_cancel(run_id: str) -> dict[str, Any]:
    run = _registry(run_id)
    if run.task and not run.task.done():
        run.task.cancel()
        return {"cancelled": True, "run_id": run_id}
    return {"cancelled": False, "run_id": run_id, "reason": "already finished"}
