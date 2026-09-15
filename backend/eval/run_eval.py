"""跑 gold set，算出可写进 README 的数字。

用法：
    # 离线模式（默认）：用本地假端点跑，验证评估链路与指标定义，不花一分钱
    cd backend && .venv/bin/python -m eval.run_eval

    # 真实模式：用你自己的模型跑（需要能访问 goldset 里的仓库地址）
    PAPERLENS_EVAL_BASE_URL=https://api.deepseek.com/v1 \
    PAPERLENS_EVAL_API_KEY=sk-xxx \
    PAPERLENS_EVAL_MODEL=deepseek-chat \
    .venv/bin/python -m eval.run_eval --provider real --label "DeepSeek-V3"

产出：
    eval/report.json   每个 gold 的明细 + 汇总
    eval/report.md     可直接贴进 README 的表格

注意：离线模式用的是**脚本化的"理想 Agent"**，它给出的高分只说明"管道和指标没写错"，
不代表真实模型的能力。别把离线数字当成项目效果贴出去。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from app.eval_metrics import aggregate, evaluate_pair, markdown_table
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


def load_goldset(path: Path) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def resolve_pdf(entry: dict[str, Any]) -> Path:
    pdf = ROOT / entry["paper"]["pdf"]
    if not pdf.exists():
        raise SystemExit(f"[{entry['id']}] 找不到论文 PDF：{pdf}\n真实论文请放到 backend/eval/pdfs/ 下。")
    return pdf


def resolve_repo_url(entry: dict[str, Any]) -> str:
    url = entry["repo"]["url"]
    if url.startswith(("http://", "https://")):
        return url
    candidate = ROOT / url
    if not candidate.exists():
        raise SystemExit(f"[{entry['id']}] 找不到本地仓库：{candidate}")
    return str(candidate.resolve())


def check_frozen_commit(entry: dict[str, Any], repo_url: str) -> tuple[bool, str]:
    """校验 goldset 里冻结的 commit 和仓库现状是否一致。

    本地夹具仓库可以直接查；https 仓库离线查不了，只能提示用户自己确认。
    这条检查存在的意义：仓库一变，之前的标注就失效了，指标会静默失真。
    """
    frozen = entry["repo"].get("commit_sha")
    if not frozen:
        return False, "goldset 里没有 commit_sha（标注纪律要求冻结它）"
    if repo_url.startswith("https://"):
        return True, f"https 仓库，离线无法核对 commit（请自行确认 {frozen[:12]}… 仍是你要测的版本）"
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_url, capture_output=True, text=True, timeout=20
    )
    head = result.stdout.strip()
    if head != frozen:
        return False, f"仓库 HEAD（{head[:12]}…）与冻结的 commit（{frozen[:12]}…）不一致 → 请重新标注"
    return True, f"commit 已冻结：{frozen[:12]}…"


async def run_one(
    client: httpx.AsyncClient,
    entry: dict[str, Any],
    provider: dict[str, Any],
    *,
    verbose: bool,
) -> dict[str, Any]:
    pdf = resolve_pdf(entry)
    repo_url = resolve_repo_url(entry)

    response = await client.post(
        "/api/runs",
        files={"file": (pdf.name, pdf.read_bytes(), "application/pdf")},
        data={"provider": json.dumps(provider)},
    )
    if response.status_code != 200:
        raise SystemExit(f"[{entry['id']}] 上传失败：{response.text[:300]}")
    run_id = response.json()["run_id"]

    baseline = await current_max_event_id(client, run_id)
    await client.post(f"/api/runs/{run_id}/recon", json={"provider": provider})
    await collect_sse(client, f"/api/runs/{run_id}/events?from_id={baseline}", since_id=baseline)

    detail = await client.get(f"/api/runs/{run_id}")
    plan = (detail.json().get("artifact") or {}).get("plan") or {}

    baseline = await current_max_event_id(client, run_id)
    started = time.monotonic()
    locate = await client.post(
        f"/api/runs/{run_id}/locate", json={"provider": provider, "repo_url": repo_url}
    )
    if locate.status_code != 200:
        raise SystemExit(f"[{entry['id']}] 阶段 B 启动失败：{locate.text[:300]}")
    await collect_sse(client, f"/api/runs/{run_id}/events?from_id={baseline}", since_id=baseline)
    wall_clock = time.monotonic() - started

    artifact = (await client.get(f"/api/runs/{run_id}")).json().get("artifact") or {}
    report = evaluate_pair(entry, plan, artifact)
    report["run_id"] = run_id
    report["wall_clock_seconds"] = round(wall_clock, 1)
    if verbose:
        print(
            f"   清单 {report['plan']['predicted']} 条（标注 {report['plan']['labeled']} 条）· "
            f"引用 {report['citations']['verified']}/{report['citations']['total']} 通过核验 · "
            f"{wall_clock:.1f}s",
            flush=True,
        )
    return report


async def main_async(args: argparse.Namespace) -> int:
    goldset = load_goldset(Path(args.goldset))
    if args.only:
        goldset = [entry for entry in goldset if entry["id"] in set(args.only)]
    if not goldset:
        raise SystemExit("gold set 为空")

    print(f"gold set：{len(goldset)} 篇 → {[entry['id'] for entry in goldset]}", flush=True)

    # ---- 冻结 commit 检查（在跑之前，而不是跑之后）----
    problems: list[str] = []
    for entry in goldset:
        ok, message = check_frozen_commit(entry, resolve_repo_url(entry))
        print(f"  [{'ok ' if ok else '!! '}] {entry['id']}: {message}", flush=True)
        if not ok:
            problems.append(f"{entry['id']}: {message}")
    if problems and not args.force:
        print("\n❌ 标注纪律检查未通过，先修好再跑（加 --force 可以跳过，但结论会不可信）：", flush=True)
        for item in problems:
            print(f"   - {item}", flush=True)
        return 2

    # ---- 起服务 ----
    procs: list[subprocess.Popen] = []
    mock_proc = None
    if args.provider == "mock":
        mock_proc = start_service("devtools.mock_provider:app", MOCK_PORT)
        procs.append(mock_proc)
    app_proc = start_service(
        "app.main:app", APP_PORT, extra_env={"PAPERLENS_ALLOW_LOCAL_REPO_PATHS": "true"}
    )
    procs.append(app_proc)

    try:
        if mock_proc:
            await wait_http(f"http://127.0.0.1:{MOCK_PORT}/health")
        await wait_http(f"{APP}/api/health")

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
            base_url = os.environ.get("PAPERLENS_EVAL_BASE_URL")
            api_key = os.environ.get("PAPERLENS_EVAL_API_KEY")
            model = os.environ.get("PAPERLENS_EVAL_MODEL")
            if not (base_url and api_key and model):
                raise SystemExit(
                    "真实模式需要三个环境变量：PAPERLENS_EVAL_BASE_URL / PAPERLENS_EVAL_API_KEY / PAPERLENS_EVAL_MODEL"
                )
            protocol = os.environ.get("PAPERLENS_EVAL_PROTOCOL", "openai-compatible")
            provider = {"protocol": protocol, "base_url": base_url, "api_key": api_key, "model": model}
            label = args.label or model
            note = ""

        reports: list[dict[str, Any]] = []
        async with httpx.AsyncClient(base_url=APP, timeout=900) as client:
            for entry in goldset:
                print(f"\n▶ {entry['id']}（{entry['paper']['title']}）", flush=True)
                reports.append(await run_one(client, entry, provider, verbose=True))
    finally:
        stop_services(*procs)

    summary = aggregate(reports)
    payload = {
        "label": label,
        "provider": {key: value for key, value in provider.items() if key != "api_key"},
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "goldset": str(Path(args.goldset).relative_to(ROOT)),
        "aggregate": summary,
        "pairs": reports,
    }
    REPORT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    table = markdown_table(summary, provider_label=label, note=note)
    REPORT_MD.write_text(table + "\n", encoding="utf-8")

    print("\n" + table, flush=True)
    print(f"\n已写入：{REPORT_JSON.relative_to(ROOT.parent)}、{REPORT_MD.relative_to(ROOT.parent)}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="跑 PaperLens gold set")
    parser.add_argument("--goldset", default=str(GOLDSET))
    parser.add_argument("--provider", choices=["mock", "real"], default="mock")
    parser.add_argument("--only", nargs="*", help="只跑指定的 gold id")
    parser.add_argument("--label", help="报告里这一行的名字（例如 DeepSeek-V3）")
    parser.add_argument("--force", action="store_true", help="跳过标注纪律检查（结论自负）")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
