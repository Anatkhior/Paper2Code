"""M3 离线验收：对照界面所需的后端能力 + 前端确实构建并渲染出来。

M3 的完成判据是"一条引用能点开看到代码原文"。这条判据拆成两半来验证：

A. 后端：点开靠两个端点（读代码 / 读论文页），它们必须
   —— 从 **git 对象**里读（和 verify.py 核验的是同一份内容，否则核验白做）
   —— 防路径穿越、区分 404/409/422、超长自动截断
B. 前端：`pnpm build` 必须过（TypeScript 全量检查），并且生产服务渲染出的页面里
   确实有"对照 / 覆盖率 / 点开看原文"这些结构。

跑之前请先停掉手动起的 dev server：`pnpm build` 与 `next dev` 共用 .next 目录。

运行：
    cd backend && .venv/bin/python -m scripts.m3_check
    PAPERLENS_SKIP_FRONTEND=1 ... # 只跑后端那半（快）
"""

from __future__ import annotations

import asyncio
import json
import os
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
    provider,
    start_service,
    stop_services,
    wait_http,
    ROOT,
)

from tests.paper_fixture import KEY_QUOTE, ensure_fixtures
from tests.repo_fixture import build_repo, layer_lines

FRONTEND = ROOT.parent / "frontend"
FRONTEND_PORT = 3300
FORWARD = layer_lines("    def forward(self, x):")


def frontend_env() -> dict[str, str]:
    """前端工具的缓存目录必须留在工作区内（这台机器的家目录是只读的）。"""
    workspace = ROOT.parent
    return {
        **os.environ,
        "XDG_CACHE_HOME": str(workspace / ".cache"),
        "XDG_CONFIG_HOME": str(workspace / ".config"),
        "XDG_DATA_HOME": str(workspace / ".local" / "share"),
        "npm_config_store_dir": str(workspace / ".pnpm-store"),
        "NEXT_TELEMETRY_DISABLED": "1",
    }


# ---------------------------------------------------------------------------
# A. 后端：点开看原文
# ---------------------------------------------------------------------------
async def section_a(check: Checker, client: httpx.AsyncClient) -> str:
    from app.repo_source import RepoSource

    check.section("A. 后端「点开看原文」端点")
    ensure_fixtures(ROOT)
    pdf = (ROOT / "tests" / "fixtures" / "synthetic_paper.pdf").read_bytes()
    created = await client.post(
        "/api/runs",
        files={"file": ("synthetic_paper.pdf", pdf, "application/pdf")},
        data={"provider": json.dumps(provider("mock-model"))},
    )
    run_id = created.json()["run_id"]

    # 还没克隆之前就该被挡住
    early = await client.get(f"/api/runs/{run_id}/file", params={"path": "loralib/layers.py"})
    check(early.status_code == 409, f"还没跑阶段 B 就点代码 → 409（HTTP {early.status_code}）")

    baseline = await current_max_event_id(client, run_id)
    await client.post(f"/api/runs/{run_id}/recon", json={"provider": provider("mock-model")})
    await collect_sse(client, f"/api/runs/{run_id}/events?from_id={baseline}", since_id=baseline)

    baseline = await current_max_event_id(client, run_id)
    await client.post(
        f"/api/runs/{run_id}/locate",
        json={"provider": provider("mock-model"), "repo_url": str(build_repo(ROOT))},
    )
    await collect_sse(client, f"/api/runs/{run_id}/events?from_id={baseline}", since_id=baseline)

    # ---- 读代码 ----
    response = await client.get(
        f"/api/runs/{run_id}/file",
        params={"path": "loralib/layers.py", "start": FORWARD[0], "end": FORWARD[1]},
    )
    check(response.status_code == 200, f"读代码成功（HTTP {response.status_code}）")
    view = response.json()
    check(
        [row["n"] for row in view["lines"]] == list(range(FORWARD[0], FORWARD[1] + 1)),
        f"行号连续且与请求一致（{view['line_start']}-{view['line_end']}）",
    )
    check(view["total_lines"] == 43, f"报告了文件总行数（{view['total_lines']}）")
    check(
        view["focus"] == {"start": FORWARD[0], "end": FORWARD[1]},
        "回显 focus 区间（前端据此高亮）",
    )

    artifact = json.loads((ROOT / "data" / run_id / "artifact.json").read_text(encoding="utf-8"))
    repo = RepoSource(ROOT / "data" / run_id / "repo", artifact["run"]["repo"]["commit_sha"])
    from_git = repo.content_at_commit("loralib/layers.py").split("\n")[FORWARD[0] - 1 : FORWARD[1]]
    check(
        [row["text"] for row in view["lines"]] == from_git,
        "返回的每一行都与 git 对象里的内容逐字一致（和核验读的是同一份）",
    )
    check(
        view["commit_sha"] == artifact["run"]["repo"]["commit_sha"],
        "返回里带上锁定 commit（用户能知道看的是哪个版本）",
    )
    check(view["source_url"] is None, "本地路径不编造托管站链接")

    # 引用的区间和界面点开的是同一段
    first_evidence = artifact["innovations"][0]["code_evidence"][0]
    check(
        first_evidence["path"] == "loralib/layers.py"
        and first_evidence["line_start"] == FORWARD[0]
        and first_evidence["line_end"] == FORWARD[1],
        "产物里的引用区间与点开看到的一致",
    )

    # ---- 护栏 ----
    traversal = await client.get(f"/api/runs/{run_id}/file", params={"path": "../../../etc/passwd"})
    check(traversal.status_code == 422, f"路径穿越 → 422（HTTP {traversal.status_code}）")

    missing = await client.get(f"/api/runs/{run_id}/file", params={"path": "loralib/ghost.py"})
    check(missing.status_code == 404, f"不存在的文件 → 404（HTTP {missing.status_code}）")

    short = await client.get(f"/api/runs/{run_id}/file", params={"path": "loralib/layers.py"})
    check(
        short.json()["truncated"] is False and len(short.json()["lines"]) == 43,
        "小文件整读不会被误标成截断",
    )
    huge = await client.get(f"/api/runs/{run_id}/file", params={"path": "utils/generated_tables.py"})
    check(
        huge.json()["truncated"] is True and len(huge.json()["lines"]) == 2000,
        f"超大文件截断到 2000 行并标注（文件共 {huge.json()['total_lines']} 行）",
    )
    check(
        huge.json()["line_start"] == 1 and huge.json()["line_end"] == 2000,
        "截断是从第一行开始给足 2000 行，而不是随便截一段",
    )

    bad_range = await client.get(
        f"/api/runs/{run_id}/file", params={"path": "loralib/layers.py", "start": 40, "end": 10}
    )
    check(bad_range.status_code == 422, f"行区间非法 → 422（HTTP {bad_range.status_code}）")

    # ---- 读论文页 ----
    page = await client.get(f"/api/runs/{run_id}/paper/page/3")
    body = page.json()
    check(page.status_code == 200 and body["page_count"] == 6, "读论文页成功（共 6 页）")
    check(KEY_QUOTE in body["text"], "第 3 页原文里包含被引用的那句话（左栏点开核对得到）")

    out_of_range = await client.get(f"/api/runs/{run_id}/paper/page/99")
    check(out_of_range.status_code == 422, f"页码越界 → 422（HTTP {out_of_range.status_code}）")

    # ---- 原版页面视图：引文高亮框 + 页面渲染图（2026-09-16 用户反馈"PDF 那边没高亮"）----
    # 为什么后端给矩形：浏览器内置 PDF 阅读器不允许外部脚本碰它的 DOM（`#search=` 在 Chrome 上
    # 也不生效），所以"在 PDF 原版里高亮同一段"只能靠"服务端渲染该页 + 我们自己叠高亮框"。
    located = await client.get(f"/api/runs/{run_id}/paper/page/3", params={"quote": KEY_QUOTE})
    located_body = located.json()
    check(
        (located_body.get("page_width") or 0) > 0 and (located_body.get("page_height") or 0) > 0,
        f"返回页面尺寸（{located_body.get('page_width')}×{located_body.get('page_height')} 点）——"
        "前端要把矩形换算成百分比才不会错位",
    )
    rects = located_body.get("highlight_rects") or []
    check(len(rects) >= 1, f"引文在这一页上被定位到（{len(rects)} 个高亮框）")
    if rects:
        x0, y0, x1, y1 = rects[0]
        check(
            0 <= x0 < x1 <= (located_body.get("page_width") or x1)
            and 0 <= y0 < y1 <= (located_body.get("page_height") or y1),
            f"矩形落在页面内且非空：{(round(x0), round(y0), round(x1), round(y1))}",
        )
    check(
        (located_body.get("highlight_coverage") or 0) > 0.99,
        f"整句引文的高亮覆盖率是 1（实际 {located_body.get('highlight_coverage')}）",
    )
    # 引文里混进公式/符号：PDF 文本层匹配不全，但必须**如实报告覆盖了多少**，
    # 而不是默默只高亮前几个词（2026-09-16 用户反馈"公式的部分高亮的并不全面"）
    partial_quote = KEY_QUOTE.replace("by four orders of magnitude", "by four orders of magnitude (alpha/r)·BAx")
    partial = await client.get(f"/api/runs/{run_id}/paper/page/3", params={"quote": partial_quote})
    partial_body = partial.json()
    check(
        0 < (partial_body.get("highlight_coverage") or 0) < 0.999,
        f"夹杂公式的引文：覆盖率如实小于 1（实际 {partial_body.get('highlight_coverage')}）",
    )
    check(
        len(partial_body.get("highlight_rects") or []) >= 1,
        f"并且仍然画出了能匹配上的那部分框（{len(partial_body.get('highlight_rects') or [])} 个）",
    )
    nothing = await client.get(
        f"/api/runs/{run_id}/paper/page/3", params={"quote": "a sentence that is definitely not in this paper"}
    )
    check(
        (nothing.json().get("highlight_rects") or []) == [],
        "找不到就返回空数组——**不许画假框**（前端会如实说明这一页没定位到）",
    )
    image = await client.get(f"/api/runs/{run_id}/paper/page/3/image", params={"dpi": 150})
    check(image.status_code == 200, f"页面渲染图可用（HTTP {image.status_code}）")
    check(
        image.headers.get("content-type", "").startswith("image/png")
        and image.content[:4] == bytes([0x89]) + b"PNG",
        f"返回的是真 PNG（{len(image.content) // 1024}KB）",
    )
    bad_image = await client.get(f"/api/runs/{run_id}/paper/page/99/image")
    check(bad_image.status_code == 422, f"渲染越界页 → 422（HTTP {bad_image.status_code}）")

    # ---- 跨行 / 连字符折行 / 公式的引文：必须覆盖折行后的部分（2026-09-17 用户反馈）----
    hard_pdf = ROOT / "data" / f"hard-page-{int(time.time() * 1000)}.pdf"
    hard_quote = build_hard_case_pdf(hard_pdf)
    hard_run = (
        await client.post(
            "/api/runs",
            files={"file": ("hard.pdf", hard_pdf.read_bytes(), "application/pdf")},
            data={"provider": json.dumps(provider("mock-model"))},
        )
    ).json()["run_id"]
    hard = (await client.get(f"/api/runs/{hard_run}/paper/page/1", params={"quote": hard_quote})).json()
    hard_rects = hard.get("highlight_rects") or []
    check(
        len(hard_rects) >= 2,
        f"跨行引文覆盖折行后的部分（{len(hard_rects)} 个框，覆盖 {hard.get('highlight_coverage')}）"
        "——旧实现只高亮前半截",
    )
    check(
        (hard.get("highlight_coverage") or 0) >= 0.6,
        f"跨行 + 连字符 + 公式的引文覆盖率仍然可观（{hard.get('highlight_coverage')}）",
    )
    bogus = (await client.get(f"/api/runs/{hard_run}/paper/page/1", params={"quote": "definitely not on this page at all"})).json()
    check(
        (bogus.get("highlight_rects") or []) == [] and not (bogus.get("highlight_coverage") or 0),
        "常见词拼出来的假引文**不画框**（几何匹配加了覆盖率与连续命中阈值，防误报）",
    )

    # ---- PDF 原版（交给浏览器自带阅读器，才能看到真实排版/公式/图）----
    pdf_response = await client.get(f"/api/runs/{run_id}/pdf")
    check(pdf_response.status_code == 200, f"PDF 直链可用（HTTP {pdf_response.status_code}）")
    check(
        pdf_response.headers.get("content-type", "").startswith("application/pdf"),
        f"Content-Type 是 application/pdf（{pdf_response.headers.get('content-type')}）",
    )
    check(
        "inline" in pdf_response.headers.get("content-disposition", ""),
        "Content-Disposition 是 inline（浏览器内嵌显示，而不是下载）",
    )
    uploaded = (ROOT / "tests" / "fixtures" / "synthetic_paper.pdf").read_bytes()
    check(pdf_response.content == uploaded, "返回的字节和上传的 PDF 完全一致（原样透传，没有被重新编码）")

    missing_pdf = await client.get("/api/runs/no-such-run/pdf")
    check(missing_pdf.status_code == 404, f"未知 run 取 PDF → 404（HTTP {missing_pdf.status_code}）")

    return run_id


# ---------------------------------------------------------------------------
# B. 前端：构建 + 真实渲染
# ---------------------------------------------------------------------------
async def section_b(check: Checker) -> None:
    check.section("B. 前端构建与渲染")

    if os.environ.get("PAPERLENS_SKIP_FRONTEND"):
        print("   （PAPERLENS_SKIP_FRONTEND 已设置，跳过）", flush=True)
        return
    if not (FRONTEND / "node_modules").exists():
        check(False, f"前端依赖没装（{FRONTEND}/node_modules 不存在），先跑 pnpm install")
        return
    if not shutil.which("pnpm"):
        check(False, "找不到 pnpm")
        return

    started = time.monotonic()
    build = subprocess.run(
        ["pnpm", "build"],
        cwd=FRONTEND,
        env=frontend_env(),
        capture_output=True,
        text=True,
        timeout=600,
    )
    check(
        build.returncode == 0,
        f"pnpm build 通过（TypeScript 全量检查 + 打包，{time.monotonic() - started:.0f}s）",
    )
    if build.returncode != 0:
        print((build.stdout + build.stderr)[-1500:], flush=True)
        return

    next_bin = FRONTEND / "node_modules" / ".bin" / "next"
    server = subprocess.Popen(
        [str(next_bin), "start", "-p", str(FRONTEND_PORT)],
        cwd=FRONTEND,
        env=frontend_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await wait_http(f"http://127.0.0.1:{FRONTEND_PORT}/", timeout=60)
        async with httpx.AsyncClient() as plain:
            html = (await plain.get(f"http://127.0.0.1:{FRONTEND_PORT}/", timeout=30)).text
        markers = {
            "1. 模型（自带 key）": "provider 表单",
            "2. 论文 PDF": "论文上传",
            "3. 创新点清单": "清单勾选区",
            "4. 代码仓库": "阶段 B 输入区",
            "5. Agent 行动轨迹": "时间线",
            "6. 对照阅读器": "双栏阅读器",
            "7. 追问": "追问面板（放在阅读器与结论之间，方便边看边问）",
            "8. 逐条结论": "逐条结论区",
            "论文原文": "阅读器左栏（论文原文）",
            "PDF 原版": "PDF 视图开关",
            "原文文本": "文本视图开关（可划选）",
            "代码实现": "阅读器右栏（代码实现）",
            "覆盖率与预算": "覆盖率/预算卡片",
            "点得开原文": "引用可点击的提示",
        }
        for marker, description in markers.items():
            check(marker in html, f"页面渲染出「{description}」（{marker}）")
        check("PaperLens" in html, "页面标题正常")

        # 版式回归（2026-09-16 用户反馈）：
        # ① 「核心创新点」下面那个定位按钮与第 4 节的重复 → 已移除，只保留一个动作入口
        check(
            "开始定位选中项" not in html,
            "创新点清单区不再有第二个「开始定位」按钮（与第 4 节重复）",
        )
        check(
            "开始定位选中的" in html,
            "第 4 节仍然保留唯一的定位入口（文案带选中条数）",
        )
        # ② 追问面板要夹在「对照阅读器」与「逐条结论」之间（用户要求的阅读/交互顺序）
        order = [html.find(name) for name in ("6. 对照阅读器", "7. 追问", "8. 逐条结论")]
        check(
            all(pos >= 0 for pos in order) and order == sorted(order),
            f"页面顺序：对照阅读器 → 追问 → 逐条结论（位置 {order}）",
        )
        # ③ 结论区的引导文案要跟新顺序一致（上面隔着追问面板）
        #    （阅读器"PDF 内置查找高亮 + 高亮自动进视野"是运行期行为，由 m6 从打包产物里断言）
        check(
            "上方的对照阅读器会跳到对应位置" in html,
            "逐条结论区的引导文案与新版式一致（点引用 → 上方阅读器跳过去）",
        )
        # ④ 行动轨迹默认收起、可随时展开（用户反馈：它占了太多正文空间）
        check(
            "展开详细轨迹" in html and 'aria-expanded="false"' in html,
            "行动轨迹默认收起，并给出「展开详细轨迹」按钮",
        )
        check(
            "收起轨迹" not in html,
            "默认状态下按钮文案是「展开」而不是「收起」（默认收起）",
        )
        # ⑤ 布局（2026-09-17 用户反馈：创新点单列太浪费、轨迹该常驻侧栏）
        check(
            "timeline-sidebar" in html and "sticky top-4" in html and "xl:block" in html,
            "行动轨迹有常驻侧栏（sticky + 固定宽度 + 宽屏才显示）——任何步骤都能看到进展",
        )
        check(
            "xl:hidden" in html,
            "窄屏退回正文里的内联轨迹（侧栏放不下时仍能看到）",
        )
        # 创新点/结论的多列网格只在有数据时渲染，属于打包产物层面的断言（见 m6）
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()


def build_hard_case_pdf(dest: Path) -> str:
    """造一页"真论文形态"的 PDF：连字符折行 + 公式 + 不换行空格。

    2026-09-17 用户实测：跨行的引文只高亮了前半截。根因是"整段/整窗字符串匹配"对空白极其敏感，
    真论文里的连字符折行、非 ASCII 空格、公式都会让它失败。这里用这一页把这个回归钉住。
    返回这一页上那句跨行引文的**逻辑写法**（人类写法：不连字符、普通空格）。
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "3.2 Low-Rank Reparameterization", fontsize=12)
    body = [
        "Low-rank reparameterization reduces the number of trainable parame-",
        "ters by four orders of magnitude, and the update is dW = BA where",
        "B\u00a0has shape (d, r) and A has shape (r, k); during training W0 is frozen.",
    ]
    y = 130
    for line in body:
        page.insert_text((72, y), line, fontsize=11)
        y += 16
    dest.parent.mkdir(parents=True, exist_ok=True)
    doc.save(dest)
    doc.close()
    return (
        "Low-rank reparameterization reduces the number of trainable parameters by four orders "
        "of magnitude, and the update is dW = BA where B has shape (d, r) and A has shape (r, k)"
    )


async def main() -> int:
    check = Checker("M3 验收")
    mock = start_service("devtools.mock_provider:app", MOCK_PORT)
    app_proc = start_service(
        "app.main:app",
        APP_PORT,
        extra_env={"PAPERLENS_ALLOW_LOCAL_REPO_PATHS": "true"},
    )
    try:
        await wait_http(f"http://127.0.0.1:{MOCK_PORT}/health")
        await wait_http(f"{APP}/api/health")
        print(f"mock provider : {MOCK_PORT}\nPaperLens API : {APP}", flush=True)

        async with httpx.AsyncClient(base_url=APP, timeout=300) as client:
            await section_a(check, client)

        await section_b(check)
        return check.finish()
    finally:
        stop_services(mock, app_proc)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
