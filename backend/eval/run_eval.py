"""跑 gold set，算出可写进 README 的数字。

用法：
    # 离线模式（默认）：用本地假端点跑，验证评估链路与指标定义，不花一分钱
    cd backend && .venv/bin/python -m eval.run_eval

    # 真实模式：用你自己的模型跑（需要能访问 goldset 里的仓库地址）
    PAPER2CODE_EVAL_BASE_URL=https://api.deepseek.com/v1 \
    PAPER2CODE_EVAL_API_KEY=sk-xxx \
    PAPER2CODE_EVAL_MODEL=deepseek-chat \
    .venv/bin/python -m eval.run_eval --provider real --label "DeepSeek-V3"

产出：
    离线模式：eval/report.json + eval/report.md（离线基线，m4_check 会核对它们和 README）
    真实模式：eval/reports/<时间>-<label>-<随机标识>.json + .md（单独存档，不会覆盖离线基线）

注意：离线模式用的是**脚本化的"理想 Agent"**，它给出的高分只说明"管道和指标没写错"，
不代表真实模型的能力。别把离线数字当成项目效果贴出去。

真实模式与离线模式的差别（都是为了让真实数字可信）：
- 每篇都按 goldset 里冻结的 commit 克隆，跑完再核对一次实际用的 commit，对不上就判无效；
- 不覆盖 backend/.env 里的节流等个人配置（离线模式为了可复现会钉住它们）；
- 一篇失败不影响其余篇目：失败的篇目单独列出、不进汇总，退出码非 0。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import yaml

from app.config_compat import canonical_env
from app.eval_metrics import aggregate, evaluate_pair, markdown_table
from app.repo_source import COMMIT_SHA_PATTERN
from scripts.harness import (
    APP,
    APP_PORT,
    MOCK_PORT,
    collect_sse,
    current_max_event_id,
    start_service,
    stop_services,
    wait_http,
    ROOT,
)

GOLDSET = ROOT / "eval" / "goldset.yaml"
REPORT_JSON = ROOT / "eval" / "report.json"
REPORT_MD = ROOT / "eval" / "report.md"
REPORTS_DIR = ROOT / "eval" / "reports"

# 单个阶段（侦察 / 定位）等事件流的上限。定位自己的墙钟预算默认 600s，
# 再加上克隆（默认上限 300s）与限流等待，给足余量；超了就取消这一篇、接着跑下一篇。
PHASE_TIMEOUT_SECONDS = 1800


class PairFailed(Exception):
    """某一篇没能产出可评分的结果（不是指标低，是根本没跑完）。"""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"{stage}：{message}")
        self.stage = stage
        self.message = message
        self.end_event: dict[str, Any] | None = None


def load_goldset(path: Path) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def resolve_pdf(entry: dict[str, Any]) -> Path:
    pdf = ROOT / entry["paper"]["pdf"]
    if not pdf.exists():
        raise PairFailed("准备", f"找不到论文 PDF：{pdf}；真实论文请放到 backend/eval/pdfs/ 下")
    return pdf


def resolve_repo_url(entry: dict[str, Any]) -> str:
    url = entry["repo"]["url"]
    if url.startswith(("http://", "https://")):
        return url
    candidate = ROOT / url
    if not candidate.exists():
        raise PairFailed("准备", f"找不到本地仓库：{candidate}")
    return str(candidate.resolve())


def check_frozen_commit(entry: dict[str, Any], repo_url: str) -> tuple[bool, str]:
    """检查冻结提交的格式与本地可达性；远端是否可取交给克隆，产物还要再核对。"""
    frozen = entry["repo"].get("commit_sha")
    if not isinstance(frozen, str) or not re.fullmatch(COMMIT_SHA_PATTERN, frozen):
        return False, "goldset 的 commit_sha 必须是完整的 40 位小写十六进制 SHA"
    if repo_url.startswith(("http://", "https://")):
        return True, f"远端仓库，将按冻结的 commit 克隆：{frozen[:12]}…"
    # 本地仓库也允许 HEAD 前进；只要冻结的提交仍在，就不需要重新标注。
    result = subprocess.run(
        ["git", "cat-file", "-t", frozen], cwd=repo_url, capture_output=True, text=True, timeout=20
    )
    if result.returncode != 0 or result.stdout.strip() != "commit":
        return False, f"本地仓库里找不到冻结的提交：{frozen[:12]}…"
    return True, f"commit 已冻结：{frozen[:12]}…"


async def _cancel_run(client: httpx.AsyncClient, run_id: str) -> str:
    try:
        response = await client.post(f"/api/runs/{run_id}/cancel", timeout=10)
        response.raise_for_status()
        return "已请求取消"
    except httpx.HTTPError:
        return "取消请求失败，服务端任务可能仍在运行"


async def _run_phase(
    client: httpx.AsyncClient,
    run_id: str,
    path: str,
    payload: dict[str, Any],
    *,
    stage: str,
    timeout_s: float,
) -> dict[str, Any]:
    """启动阶段并等待 run_end；超时或断流都要尝试取消，不能留下付费任务。"""
    baseline = await current_max_event_id(client, run_id)
    try:
        async with asyncio.timeout(timeout_s):
            response = await client.post(path, json=payload)
            if response.status_code != 200:
                raise PairFailed(stage, f"启动失败（HTTP {response.status_code}）：{response.text[:300]}")
            events, _, _ = await collect_sse(
                client, f"/api/runs/{run_id}/events?from_id={baseline}", since_id=baseline, timeout_s=timeout_s
            )
            ends = [event for event in events if event["type"] == "run_end"]
            if not ends:
                raise PairFailed(stage, "事件流在 run_end 之前断开")
            return ends[-1]
    except (TimeoutError, httpx.HTTPError, PairFailed, ValueError) as exc:
        cancellation = await _cancel_run(client, run_id)
        if isinstance(exc, TimeoutError):
            message = f"超过 {timeout_s:g}s 没有结束"
        elif isinstance(exc, PairFailed):
            message = exc.message
        else:
            message = f"{type(exc).__name__}: {exc}"
        failure = PairFailed(stage, f"{message}；{cancellation}")
        # 断流不等于服务端没落盘；尽力回收本阶段的结束事件，不能误用上一阶段的用量。
        try:
            detail = await client.get(f"/api/runs/{run_id}", timeout=10)
            detail.raise_for_status()
            ends = [event for event in detail.json().get("events", [])
                    if event["type"] == "run_end" and event["id"] > baseline]
            failure.end_event = ends[-1] if ends else None
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        raise failure from None


def _run_end_summary(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") or event
    reason = data.get("stopped_reason")
    return {
        "status": data.get("status"),
        "stopped_reason": reason,
        "partial": bool(
            data.get("partial") or data.get("missing_ids")
            or data.get("status") != "ok" or data.get("finalize_error")
            or (reason and reason not in {"agent_finished", "Agent 主动结束", "模型没有继续调用工具"})
        ),
        "usage": data.get("usage") or {},
    }


def _total_usage(phases: dict[str, Any]) -> dict[str, Any]:
    # 沿用后端预算口径：输入优先用端点值、缺失时估算；输出是文本估算，不是账单。
    result = {}
    for key in ("tool_calls", "input_tokens", "output_tokens", "seconds"):
        values = [phase["usage"][key] for phase in phases.values() if phase.get("usage", {}).get(key) is not None]
        result[key] = round(sum(values), 1) if values else None
    return result


def _error_text(exc: Exception, provider: dict[str, Any]) -> str:
    text = exc.message if isinstance(exc, PairFailed) else f"{type(exc).__name__}: {exc}"
    key = provider.get("api_key")
    return (text.replace(key, "[REDACTED]") if key else text)[:500]


async def run_one(
    client: httpx.AsyncClient,
    entry: dict[str, Any],
    provider: dict[str, Any],
    *,
    timeout_s: float,
    verbose: bool,
    force: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    report: dict[str, Any] = {"id": entry["id"], "status": "failed", "phases": {}}
    stage = "准备"
    try:
        pdf = resolve_pdf(entry)
        repo_url = resolve_repo_url(entry)
        frozen = entry["repo"].get("commit_sha")
        frozen_ok, message = check_frozen_commit(entry, repo_url)
        report["expected_commit_sha"] = frozen
        if not frozen_ok:
            report.update(status="invalid", stage=stage, error=message)
            if not force:
                return report
        response = await client.post(
            "/api/runs",
            files={"file": (pdf.name, pdf.read_bytes(), "application/pdf")},
            data={"provider": json.dumps(provider)},
        )
        if response.status_code != 200:
            raise PairFailed("上传", response.text[:300])
        run_id = response.json()["run_id"]
        report["run_id"] = run_id

        stage = "侦察"
        recon_end = await _run_phase(
            client, run_id, f"/api/runs/{run_id}/recon", {"provider": provider}, stage=stage, timeout_s=timeout_s
        )
        report["phases"]["recon"] = _run_end_summary(recon_end)
        detail = await client.get(f"/api/runs/{run_id}")
        detail.raise_for_status()
        plan = (detail.json().get("artifact") or {}).get("plan") or {}
        if not plan.get("innovations"):
            reason = report["phases"]["recon"]["stopped_reason"] or "没有产出创新点清单"
            raise PairFailed(stage, reason)

        stage = "定位"
        locate_started = time.monotonic()
        locate_payload: dict[str, Any] = {"provider": provider, "repo_url": repo_url}
        if frozen:
            locate_payload["commit"] = frozen
        locate_end = await _run_phase(
            client, run_id, f"/api/runs/{run_id}/locate", locate_payload, stage=stage, timeout_s=timeout_s
        )
        report["phases"]["locate"] = _run_end_summary(locate_end)
        report["run_end"] = report["phases"]["locate"]
        report["locate_wall_clock_seconds"] = round(time.monotonic() - locate_started, 1)
        detail = await client.get(f"/api/runs/{run_id}")
        detail.raise_for_status()
        artifact = detail.json().get("artifact") or {}
        # 没有 artifact.json 时接口退回 plan.json，不能把它当成定位结果评分。
        if "missing_ids" not in artifact:
            reason = report["run_end"]["stopped_reason"] or "没有产出交付物"
            raise PairFailed(stage, reason)

        stage = "评分"
        report.update(evaluate_pair(entry, plan, artifact))
        report["status"] = "ok"
        # --force 只允许诊断性运行，不能让没有冻结版本的结果混入正式分数。
        if not frozen_ok or report.get("commit_sha") != frozen:
            report.update(status="invalid", error=(
                f"实际分析的 commit（{report.get('commit_sha')}）与有效的冻结提交（{frozen}）不一致"
            ))
    except Exception as exc:  # noqa: BLE001 —— 每篇独立落盘；包括输入/解析错误，不能抹掉之前的成果
        if isinstance(exc, PairFailed) and exc.end_event is not None:
            phase = "recon" if stage == "侦察" else "locate"
            report["phases"][phase] = _run_end_summary(exc.end_event)
        report.update(
            status="failed", stage=exc.stage if isinstance(exc, PairFailed) else stage,
            error=_error_text(exc, provider),
        )
    finally:
        report["wall_clock_seconds"] = round(time.monotonic() - started, 1)
        report["usage"] = _total_usage(report["phases"])
        report["usage_complete"] = len(report["phases"]) == 2 and all(
            phase["usage"].get(key) is not None
            for phase in report["phases"].values()
            for key in ("tool_calls", "input_tokens", "output_tokens", "seconds")
        )
        report["partial"] = any(phase["partial"] for phase in report["phases"].values())
        if verbose:
            print(
                f"   {report['status']} · {report['wall_clock_seconds']}s"
                + ("（提前交付）" if report["partial"] else "")
                + (f" · {report['error']}" if report.get("error") else ""),
                flush=True,
            )
    return report


def _report_paths(args: argparse.Namespace, label: str) -> tuple[Path, Path]:
    """离线基线写固定位置（m4_check 与 README 对它）；真实模式按次存档，互不覆盖。"""
    if args.provider == "mock":
        return REPORT_JSON, REPORT_MD
    slug = re.sub(r"[^\w.-]+", "-", label).strip("-.")[:48] or "real"
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}-{uuid4().hex[:8]}"
    return REPORTS_DIR / f"{stem}.json", REPORTS_DIR / f"{stem}.md"


def _write_report(
    args: argparse.Namespace,
    label: str,
    note: str,
    provider: dict[str, Any],
    reports: list[dict[str, Any]],
    paths: tuple[Path, Path],
    *,
    pairs_requested: int,
) -> dict[str, Any]:
    scored = [report for report in reports if report.get("status") == "ok"]
    summary = aggregate(scored)
    summary.update(
        pairs_requested=pairs_requested,
        pairs_attempted=len(reports),
        pairs_partial=sum(1 for report in scored if report.get("partial")),
        pairs_unscored=[
            {"id": report["id"], "status": report["status"], "error": report.get("error")}
            for report in reports if report.get("status") != "ok"
        ],
        # 效果指标只汇总有效篇目；耗时与已知用量包括失败篇目和侦察，不能把花掉的钱藏起来。
        usage_total=_total_usage({str(i): {"usage": report.get("usage", {})} for i, report in enumerate(reports)}),
        wall_clock_seconds_total=round(sum(report.get("wall_clock_seconds", 0) for report in reports), 1),
        pairs_usage_incomplete=sum(1 for report in reports if not report.get("usage_complete")),
    )
    goldset_path = Path(args.goldset).resolve()
    payload = {
        "label": label,
        "provider": {key: value for key, value in provider.items() if key != "api_key"},
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "goldset": str(goldset_path.relative_to(ROOT)) if goldset_path.is_relative_to(ROOT) else str(goldset_path),
        "aggregate": summary,
        "pairs": reports,
    }
    table = markdown_table(summary, provider_label=label, note=note)
    for path, content in zip(paths, (json.dumps(payload, ensure_ascii=False, indent=2), table + "\n")):
        if provider.get("api_key"):
            content = content.replace(provider["api_key"], "[REDACTED]")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)
    return summary


async def main_async(args: argparse.Namespace) -> int:
    goldset = load_goldset(Path(args.goldset))
    if args.only:
        goldset = [entry for entry in goldset if entry["id"] in set(args.only)]
    if not goldset:
        raise SystemExit("gold set 为空")

    print(f"gold set：{len(goldset)} 篇 → {[entry['id'] for entry in goldset]}", flush=True)

    if not 0 < args.phase_timeout < float("inf"):
        raise SystemExit("--phase-timeout 必须是有限的正数")

    # ---- 模型端点（先于起服务：缺配置就别白起两个进程）----
    if args.provider == "mock":
        provider = {
            "protocol": "openai-compatible",
            "base_url": f"http://127.0.0.1:{MOCK_PORT}/v1",
            "api_key": "mock-key",
            "model": "mock-model",
        }
        label = args.label or "mock（脚本化理想 Agent）"
        note = (
            "> ⚠️ 这一行是**离线模式**：模型行为由 `devtools/mock_provider.py` 脚本化，"
            "用来验证评估链路与指标定义本身，**不代表真实模型的能力**。\n"
            "> 真实数字请用 `--provider real` 跑你自己的模型。"
        )
    else:
        env = canonical_env(os.environ)
        base_url = env.get("PAPER2CODE_EVAL_BASE_URL")
        api_key = env.get("PAPER2CODE_EVAL_API_KEY")
        model = env.get("PAPER2CODE_EVAL_MODEL")
        if not (base_url and api_key and model):
            raise SystemExit(
                "真实模式需要三个环境变量：PAPER2CODE_EVAL_BASE_URL / PAPER2CODE_EVAL_API_KEY / PAPER2CODE_EVAL_MODEL"
            )
        protocol = env.get("PAPER2CODE_EVAL_PROTOCOL", "openai-compatible")
        provider = {"protocol": protocol, "base_url": base_url, "api_key": api_key, "model": model}
        label = args.label or model
        note = ""

    # 路径每轮只生成一次；每篇之后原子更新，后续崩溃也不会抹掉已完成的结果。
    paths = _report_paths(args, label)
    reports: list[dict[str, Any]] = []
    procs: list[subprocess.Popen] = []
    try:
        if args.provider == "mock":
            procs.append(start_service("devtools.mock_provider:app", MOCK_PORT))
        procs.append(start_service(
            "app.main:app", APP_PORT,
            extra_env={"PAPER2CODE_ALLOW_LOCAL_REPO_PATHS": "true"},
            pin_local_config=args.provider == "mock",
        ))
        if args.provider == "mock":
            await wait_http(f"http://127.0.0.1:{MOCK_PORT}/health")
        await wait_http(f"{APP}/api/health")

        async with httpx.AsyncClient(base_url=APP, timeout=900) as client:
            for entry in goldset:
                print(f"\n▶ {entry['id']}（{entry.get('paper', {}).get('title', '')}）", flush=True)
                reports.append(await run_one(
                    client, entry, provider, timeout_s=args.phase_timeout, verbose=True, force=args.force
                ))
                summary = _write_report(args, label, note, provider, reports, paths, pairs_requested=len(goldset))
    finally:
        stop_services(*procs)

    print("\n" + markdown_table(summary, provider_label=label, note=note), flush=True)
    print(f"\n已写入：{paths[0]}、{paths[1]}", flush=True)
    if summary["pairs_unscored"]:
        print(f"\n⚠️ {len(summary['pairs_unscored'])} 篇没有进入汇总（见上方表格与报告里的 pairs_unscored）", flush=True)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="跑 Paper2code gold set")
    parser.add_argument("--goldset", default=str(GOLDSET))
    parser.add_argument("--provider", choices=["mock", "real"], default="mock")
    parser.add_argument("--only", nargs="*", help="只跑指定的 gold id")
    parser.add_argument("--label", help="报告里这一行的名字（例如 DeepSeek-V3）")
    parser.add_argument("--force", action="store_true", help="标注检查失败也尝试诊断性运行，但无效版本仍不计分")
    parser.add_argument(
        "--phase-timeout",
        type=float,
        default=PHASE_TIMEOUT_SECONDS,
        help=f"侦察/定位各自最多等多少秒，超时取消这一篇（默认 {PHASE_TIMEOUT_SECONDS}）",
    )
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
