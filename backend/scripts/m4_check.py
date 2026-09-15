"""M4 离线验收：gold set + 指标实现 + 数字进 README。

这个脚本的重点不是"跑一遍拿个好看的数字"，而是**证明数字不会骗人**：

A. 指标自检 —— 拿手工构造的产物喂给指标函数：
   答对时必须给高分，**答错时必须给低分**。一个只会输出 100% 的指标等于没有指标。
B. goldset 卫生 —— 每条标注必须有"要求找到的文件"或"确实不存在"；冻结的 commit 必须还在。
C. 端到端 —— 真跑一遍 `eval/run_eval.py`，校验 report.json 的结构与数值。
D. README 一致性 —— README 里贴的数字必须和 report.json 对得上，且必须标明"离线不代表真实模型能力"。

运行：
    cd backend && .venv/bin/python -m scripts.m4_check
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .harness import Checker, ROOT

GOLDSET = ROOT / "eval" / "goldset.yaml"
REPORT = ROOT / "eval" / "report.json"
REPORT_MD = ROOT / "eval" / "report.md"
README = ROOT.parent / "README.md"


# ---------------------------------------------------------------------------
# A. 指标实现自检
# ---------------------------------------------------------------------------
GOLD = {
    "id": "unit",
    "innovations": [
        {
            "name": "低秩重参数化",
            "aliases": ["low-rank", "lora"],
            "must_find_files": ["a.py"],
            "must_find_symbols": ["Foo.bar"],
        },
        {"name": "零初始化", "aliases": ["init"], "must_find_files": ["b.py"]},
        {"name": "冻结主干", "aliases": ["freeze"], "expect_not_found": True},
    ],
}

PLAN = {
    "innovations": [
        {"id": "p1", "name": "低秩分解", "one_liner": "把更新写成低秩乘积", "search_hints": ["lora_A"]},
        {"id": "p2", "name": "初始化方案", "one_liner": "init 为零"},
        {"id": "p3", "name": "冻结策略", "one_liner": "freeze 主干"},
    ]
}


def _artifact(innovations: list[dict[str, Any]], *, verified: int = 2, total: int = 2) -> dict[str, Any]:
    return {
        "innovations": innovations,
        "verification": {
            "citations_total": total,
            "citations_verified": verified,
            "citations_failed": total - verified,
            "citation_verifiable_rate": round(verified / total, 4) if total else 0.0,
        },
        "run": {
            "repo": {"commit_sha": "deadbeef"},
            "budget_used": {"tool_calls": 10, "input_tokens": 1000, "seconds": 5},
        },
    }


ARTIFACT_OK = _artifact(
    [
        {"id": "p1", "status": "matched", "code_evidence": [{"path": "a.py", "symbol": "Foo.bar"}]},
        {"id": "p2", "status": "matched", "code_evidence": [{"path": "b.py"}]},
        {"id": "p3", "status": "not_found"},
    ]
)


def section_a(check: Checker) -> None:
    from app.eval_metrics import aggregate, align, evaluate_pair, markdown_table, match_innovations

    check.section("A. 指标自检（答对给高分，答错必须给低分）")

    good = evaluate_pair(GOLD, PLAN, ARTIFACT_OK)
    check(good["plan"]["recall"] == 1.0, f"清单召回 100%（{good['plan']['matched']}/{good['plan']['labeled']}）")
    check(good["plan"]["precision"] == 1.0, "清单准确 100%（没有多报无关创新点）")
    check(good["localization"]["file_recall"] == 1.0, "文件级召回 100%")
    check(good["localization"]["file_precision"] == 1.0, "文件级准确 100%")
    check(good["localization"]["symbol_recall"] == 1.0, "符号级召回 100%")
    check(good["honesty"]["rate"] == 1.0, "应报'未找到'的条目被正确报出")
    check(good["citations"]["rate"] == 1.0, "引用核验率取自媒体里的核验结果")

    # --- 答错时必须掉分 ---
    wrong_path = evaluate_pair(
        GOLD,
        PLAN,
        _artifact(
            [
                {"id": "p1", "status": "matched", "code_evidence": [{"path": "z.py"}]},
                {"id": "p2", "status": "matched", "code_evidence": [{"path": "b.py"}]},
                {"id": "p3", "status": "not_found"},
            ]
        ),
    )
    check(
        wrong_path["localization"]["file_recall"] == 0.5,
        f"指向错文件时文件召回掉到 50%（{wrong_path['localization']['file_recall_hit']}/"
        f"{wrong_path['localization']['file_recall_total']}）",
    )
    check(
        wrong_path["localization"]["file_precision"] == 0.5,
        "引用无关文件时准确率同样掉到 50%",
    )

    missed_symbol = evaluate_pair(
        GOLD,
        PLAN,
        _artifact(
            [
                {"id": "p1", "status": "matched", "code_evidence": [{"path": "a.py", "symbol": "Something.else"}]},
                {"id": "p2", "status": "matched", "code_evidence": [{"path": "b.py"}]},
                {"id": "p3", "status": "not_found"},
            ]
        ),
    )
    check(
        missed_symbol["localization"]["symbol_recall"] == 0.0,
        "文件对但符号错 → 符号级召回 0（不因为文件蒙对就给分）",
    )
    check(
        missed_symbol["localization"]["file_recall"] == 1.0,
        "同一份结果里文件级召回仍是 100%（两个指标各算各的）",
    )

    dishonest = evaluate_pair(
        GOLD,
        PLAN,
        _artifact(
            [
                {"id": "p1", "status": "matched", "code_evidence": [{"path": "a.py", "symbol": "Foo.bar"}]},
                {"id": "p2", "status": "matched", "code_evidence": [{"path": "b.py"}]},
                # 本该说"没有实现"，却编了一个出来
                {"id": "p3", "status": "matched", "code_evidence": [{"path": "fake.py"}]},
            ]
        ),
    )
    check(
        dishonest["honesty"]["rate"] == 0.0,
        "本该报'未找到'却编了一个实现 → 诚实性得 0（这是最该被惩罚的行为）",
    )
    check(
        dishonest["localization"]["file_precision"] < 1.0,
        "编造的实现同时拉低了文件准确率",
    )

    over_reported = evaluate_pair(
        GOLD,
        {"innovations": PLAN["innovations"] + [{"id": "p4", "name": "学习率调度", "one_liner": "warmup"}]},
        ARTIFACT_OK,
    )
    check(
        over_reported["plan"]["precision"] == 0.75 and over_reported["plan"]["recall"] == 1.0,
        "多报一条无关创新点 → 准确率 75%，召回不受影响",
    )
    check(
        over_reported["plan"]["unmatched_predictions"] == ["学习率调度"],
        "报告里点名了没对上的那一条（不做黑箱）",
    )

    under_reported = evaluate_pair(GOLD, {"innovations": PLAN["innovations"][:2]}, ARTIFACT_OK)
    check(
        abs(under_reported["plan"]["recall"] - 0.6667) < 0.001,
        f"漏报一条 → 清单召回 {under_reported['plan']['recall']}",
    )
    check(
        under_reported["localization"]["file_recall"] == 1.0,
        "漏掉的那条是'本该没有实现'，所以文件召回不受影响（两个指标各算各的）",
    )
    check(
        under_reported["honesty"]["rate"] == 0.0,
        "漏报'本该没有实现'的那条 → 诚实性得 0（该说的没说，也算不诚实）",
    )

    # --- 对齐逻辑 ---
    check(
        match_innovations({"aliases": [], "must_find_files": ["a.py"]}, {"code_evidence": [{"path": "a.py"}]}),
        "没有别名时，靠'引用落在要求的文件里'也能对齐",
    )
    check(
        match_innovations({"aliases": ["Foo.bar"]}, {"name": "foo_bar_impl"}),
        "对齐忽略大小写与下划线/点号差异",
    )
    mapping, unmatched = align(
        [{"aliases": ["x"]}, {"aliases": ["x"]}],
        [{"name": "x"}, {"name": "y"}],
    )
    check(
        len(mapping) == 1 and unmatched == {1},
        "一个预测只能对上一条标注（一对多对齐会让指标虚高）",
    )

    summary = aggregate([good, wrong_path])
    check(
        summary["file_recall"] == 0.75 and summary["citations_total"] == 4,
        f"汇总用宏平均（{summary['file_recall']}）与求和（{summary['citations_total']}）分别处理",
    )
    table = markdown_table(summary, provider_label="unit")
    check("引用可核验率" in table and "|" in table, "能生成可直接贴进 README 的表格")


# ---------------------------------------------------------------------------
# B. goldset 卫生
# ---------------------------------------------------------------------------
def section_b(check: Checker) -> list[dict[str, Any]]:
    import yaml

    check.section("B. goldset 卫生与标注纪律")
    goldset = yaml.safe_load(GOLDSET.read_text(encoding="utf-8"))
    check(bool(goldset), f"goldset 能解析（{len(goldset)} 篇）")

    for entry in goldset:
        innovations = entry.get("innovations") or []
        check(bool(innovations), f"[{entry['id']}] 至少标注了 1 条创新点")
        for index, item in enumerate(innovations):
            labeled = bool(item.get("must_find_files") or item.get("must_find_symbols"))
            expect_none = bool(item.get("expect_not_found"))
            check(
                labeled != expect_none,
                f"[{entry['id']}.{index}] 「{item.get('name')}」要么给必须找到的文件，要么明确标 expect_not_found",
            )
            if labeled:
                check(bool(item.get("aliases")), f"[{entry['id']}.{index}] 有 aliases（否则无法和系统清单对齐）")

        frozen = (entry.get("repo") or {}).get("commit_sha")
        check(bool(frozen), f"[{entry['id']}] 冻结了 commit（标注纪律的硬要求）")
        url = entry["repo"]["url"]
        if not url.startswith("https://"):
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ROOT / url, capture_output=True, text=True, timeout=20
            )
            check(
                result.stdout.strip() == frozen,
                f"[{entry['id']}] 本地仓库 HEAD 与冻结 commit 一致（{frozen[:12]}…）",
            )
    return goldset


# ---------------------------------------------------------------------------
# C. 端到端跑一遍 run_eval
# ---------------------------------------------------------------------------
def section_c(check: Checker) -> dict[str, Any]:
    check.section("C. 端到端：跑一遍 run_eval（离线模式）")
    result = subprocess.run(
        [sys.executable, "-m", "eval.run_eval"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    check(result.returncode == 0, f"run_eval 退出码 0（实际 {result.returncode}）")
    if result.returncode != 0:
        print((result.stdout + result.stderr)[-1500:], flush=True)
        return {}

    payload = json.loads(REPORT.read_text(encoding="utf-8"))
    summary = payload["aggregate"]
    check(summary["pairs"] >= 1, f"报告里有 {summary['pairs']} 篇的结果")
    check(summary["citation_verifiable_rate"] == 1.0, f"离线基线引用核验率 {summary['citation_verifiable_rate']}")
    check(summary["honest_not_found_rate"] == 1.0, "离线基线的'诚实报未找到'为 100%")
    check(summary["file_recall"] == 1.0, f"离线基线文件召回 {summary['file_recall']}")

    pair = payload["pairs"][0]
    check("per_innovation" in pair and len(pair["per_innovation"]) >= 1, "明细里逐条列出了标注与产出的对应关系")
    check(pair["citations"]["total"] >= 1, f"{pair['citations']['total']} 条引用进入了统计")
    check("api_key" not in json.dumps(payload), "报告里没有泄漏 api_key")
    check(REPORT_MD.exists() and "引用可核验率" in REPORT_MD.read_text(encoding="utf-8"), "生成了 Markdown 表格")
    check("不代表真实模型" in REPORT_MD.read_text(encoding="utf-8"), "离线数字旁边明确标注了它的局限")
    return payload


# ---------------------------------------------------------------------------
# D. README 数字一致性
# ---------------------------------------------------------------------------
def section_d(check: Checker, payload: dict[str, Any]) -> None:
    check.section("D. README 里的数字与报告一致")
    if not payload:
        check(False, "没有报告可对比")
        return
    readme = README.read_text(encoding="utf-8")
    summary = payload["aggregate"]

    check("## 评估数字" in readme or "## 评估" in readme, "README 里有评估章节")
    check("引用可核验率" in readme, "README 里列出了引用核验率")
    expected = f"{summary['citation_verifiable_rate'] * 100:.0f}%"
    check(expected in readme, f"README 里的核验率与报告一致（{expected}）")
    check(
        "不代表真实模型" in readme,
        "README 明确写了「离线数字不代表真实模型能力」（不让读者误读）",
    )
    check(
        "commit" in readme.lower() and "冻结" in readme,
        "README 说明了'冻结 commit + 先标注后跑'的标注纪律",
    )


def main() -> int:
    check = Checker("M4 验收")
    section_a(check)
    section_b(check)
    payload = section_c(check)
    section_d(check, payload)
    return check.finish()


if __name__ == "__main__":
    raise SystemExit(main())
