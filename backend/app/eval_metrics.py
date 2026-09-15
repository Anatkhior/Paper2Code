"""评估指标：把"解读准不准"变成几个**可计算、可复现、不依赖模型判断**的数字。

设计原则：
1. **不用模型评判模型**。所有指标都是从产物里数出来的（文件、符号、核验状态），不是让另一个 LLM 打分。
2. **指标实现本身要被测试**。`m4_check` 会拿手工构造的产物去喂养这些函数，
   验证"答对时得高分、答错时得低分"——否则数字可能错得很讨人喜欢。
3. **分母写清楚**。每个比率都返回分子分母，避免"100%"其实是"1/1"这种误导。

对齐方式（人工标注 vs 系统产出）刻意保持朴素：
先按 aliases 关键词匹配，再退回"代码引用落在标注要求的文件里"。
匹配结果会原样写进报告，让人一眼能看出对齐是否合理——不做黑箱。
"""

from __future__ import annotations

import re
from typing import Any

_NON_WORD = re.compile(r"[\s_\-./]+")


def normalize(text: str) -> str:
    return _NON_WORD.sub("", (text or "").lower())


def _haystack(predicted: dict[str, Any]) -> str:
    parts = [
        predicted.get("name") or "",
        predicted.get("one_liner") or "",
        " ".join(predicted.get("search_hints") or []),
        (predicted.get("explanation") or {}).get("intuition") or "",
    ]
    return normalize(" ".join(parts))


def _predicted_paths(predicted: dict[str, Any]) -> set[str]:
    return {
        str(evidence.get("path") or "")
        for evidence in (predicted.get("code_evidence") or [])
        if evidence.get("path")
    }


def match_innovations(labeled: dict[str, Any], predicted: dict[str, Any]) -> bool:
    """人工标注的这一条，和被系统输出的那一条，是不是同一件事。"""
    haystack = _haystack(predicted)
    for alias in labeled.get("aliases") or []:
        if normalize(alias) and normalize(alias) in haystack:
            return True
    required = {str(item) for item in (labeled.get("must_find_files") or [])}
    return bool(required & _predicted_paths(predicted))


def align(gold_innovations: list[dict], predictions: list[dict]) -> tuple[dict[int, int], set[int]]:
    """把标注的创新点对齐到系统输出的创新点。

    返回 (对齐表 {标注下标: 预测下标}, 未被任何标注命中的预测下标集合)。
    一对一：先到先得，避免一个预测命中两条标注导致虚高。
    """
    mapping: dict[int, int] = {}
    used: set[int] = set()
    for gold_index, labeled in enumerate(gold_innovations):
        for pred_index, predicted in enumerate(predictions):
            if pred_index in used:
                continue
            if match_innovations(labeled, predicted):
                mapping[gold_index] = pred_index
                used.add(pred_index)
                break
    unmatched_predictions = {index for index in range(len(predictions))} - used
    return mapping, unmatched_predictions


def evaluate_pair(gold: dict[str, Any], plan: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    """算一篇 gold 的指标。plan 是阶段 A 的清单，artifact 是阶段 B 的产物。"""
    gold_innovations = gold.get("innovations") or []
    predictions = (plan or {}).get("innovations") or []
    mapping, unmatched = align(gold_innovations, predictions)

    # ---- 阶段 A：清单本身对不对 ----
    plan_metrics = {
        "labeled": len(gold_innovations),
        "predicted": len(predictions),
        "matched": len(mapping),
        "recall": round(len(mapping) / len(gold_innovations), 4) if gold_innovations else 0.0,
        "precision": round(len(mapping) / len(predictions), 4) if predictions else 0.0,
        "unmatched_predictions": [predictions[index].get("name") for index in sorted(unmatched)],
    }

    # ---- 阶段 B：定位与引用 ----
    by_id = {item.get("id"): item for item in (artifact or {}).get("innovations", [])}
    required_files_total = 0
    required_files_hit = 0
    predicted_files_total = 0
    predicted_files_in_scope = 0
    required_symbols_total = 0
    required_symbols_hit = 0
    not_found_expected = 0
    not_found_correct = 0
    per_innovation: list[dict[str, Any]] = []

    for gold_index, labeled in enumerate(gold_innovations):
        predicted_index = mapping.get(gold_index)
        predicted = predictions[predicted_index] if predicted_index is not None else None
        finding = by_id.get(predicted.get("id")) if predicted else None

        required = [str(item) for item in (labeled.get("must_find_files") or [])]
        paths = _predicted_paths(finding or {})
        symbols = {
            normalize(str(evidence.get("symbol") or ""))
            for evidence in (finding or {}).get("code_evidence") or []
            if evidence.get("symbol")
        }

        if labeled.get("expect_not_found") or (not required and not labeled.get("must_find_symbols")):
            not_found_expected += 1
            if finding is not None and finding.get("status") == "not_found":
                not_found_correct += 1
            # 这一条本该"没有实现"。此时任何代码引用都是无中生有，
            # 把它们计入准确率的分母（但永远不算命中）——否则编造不会被惩罚。
            predicted_files_total += len(paths)
        else:
            required_files_total += len(required)
            required_files_hit += len(set(required) & paths)
            predicted_files_total += len(paths)
            predicted_files_in_scope += len(paths & set(required))
            for symbol in labeled.get("must_find_symbols") or []:
                required_symbols_total += 1
                if any(normalize(str(symbol)) in candidate for candidate in symbols):
                    required_symbols_hit += 1

        per_innovation.append(
            {
                "labeled": labeled.get("name"),
                "predicted": (predicted or {}).get("name"),
                "status": (finding or {}).get("status"),
                "predicted_paths": sorted(paths),
                "required_paths": required,
            }
        )

    localization = {
        "file_recall": round(required_files_hit / required_files_total, 4) if required_files_total else None,
        "file_recall_hit": required_files_hit,
        "file_recall_total": required_files_total,
        "file_precision": round(predicted_files_in_scope / predicted_files_total, 4)
        if predicted_files_total
        else None,
        "file_precision_hit": predicted_files_in_scope,
        "file_precision_total": predicted_files_total,
        "symbol_recall": round(required_symbols_hit / required_symbols_total, 4)
        if required_symbols_total
        else None,
        "symbol_recall_hit": required_symbols_hit,
        "symbol_recall_total": required_symbols_total,
    }

    verification = (artifact or {}).get("verification") or {}
    citations = {
        "total": verification.get("citations_total", 0),
        "verified": verification.get("citations_verified", 0),
        "failed": verification.get("citations_failed", 0),
        "rate": verification.get("citation_verifiable_rate"),
    }

    honesty = {
        "expected_not_found": not_found_expected,
        "correct_not_found": not_found_correct,
        "rate": round(not_found_correct / not_found_expected, 4) if not_found_expected else None,
    }

    budget = ((artifact or {}).get("run") or {}).get("budget_used") or {}
    cost = {
        "tool_calls": budget.get("tool_calls"),
        "input_tokens": budget.get("input_tokens"),
        "seconds": budget.get("seconds"),
    }

    return {
        "id": gold.get("id"),
        "commit_sha": ((artifact or {}).get("run") or {}).get("repo", {}).get("commit_sha"),
        "plan": plan_metrics,
        "localization": localization,
        "citations": citations,
        "honesty": honesty,
        "cost": cost,
        "per_innovation": per_innovation,
    }


def aggregate(pair_reports: list[dict[str, Any]]) -> dict[str, Any]:
    """把多篇的指标汇总。比率做宏平均，计数做求和；分母为 None 的项不参与平均。"""

    def macro(path: tuple[str, str]) -> float | None:
        values = [
            report[path[0]][path[1]]
            for report in pair_reports
            if report.get(path[0], {}).get(path[1]) is not None
        ]
        return round(sum(values) / len(values), 4) if values else None

    def total(path: tuple[str, str]) -> int:
        return sum(report.get(path[0], {}).get(path[1]) or 0 for report in pair_reports)

    return {
        "pairs": len(pair_reports),
        "plan_recall": macro(("plan", "recall")),
        "plan_precision": macro(("plan", "precision")),
        "file_recall": macro(("localization", "file_recall")),
        "file_precision": macro(("localization", "file_precision")),
        "symbol_recall": macro(("localization", "symbol_recall")),
        "citation_verifiable_rate": macro(("citations", "rate")),
        "honest_not_found_rate": macro(("honesty", "rate")),
        "citations_total": total(("citations", "total")),
        "citations_verified": total(("citations", "verified")),
        "tool_calls_total": total(("cost", "tool_calls")),
        "seconds_total": round(total(("cost", "seconds")), 1),
    }


def markdown_table(aggregate_report: dict[str, Any], *, provider_label: str, note: str = "") -> str:
    """生成能直接贴进 README 的表格。"""
    rows = [
        ("创新点清单召回（plan recall）", aggregate_report.get("plan_recall")),
        ("创新点清单准确（plan precision）", aggregate_report.get("plan_precision")),
        ("文件级召回（file recall）", aggregate_report.get("file_recall")),
        ("文件级准确（file precision）", aggregate_report.get("file_precision")),
        ("符号级召回（symbol recall）", aggregate_report.get("symbol_recall")),
        ("**引用可核验率**", aggregate_report.get("citation_verifiable_rate")),
        ("诚实报「未找到」", aggregate_report.get("honest_not_found_rate")),
    ]
    lines = [
        f"| 指标 | {provider_label} |",
        "|---|---|",
    ]
    for label, value in rows:
        lines.append(f"| {label} | {'—' if value is None else f'{value * 100:.0f}%'} |")
    lines.append(
        f"| 引用总数 / 通过核验 | {aggregate_report.get('citations_verified')}/{aggregate_report.get('citations_total')} |"
    )
    lines.append(
        f"| 平均工具调用 / 总用时 | {aggregate_report.get('tool_calls_total')} 次 / {aggregate_report.get('seconds_total')}s |"
    )
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


__all__ = ["evaluate_pair", "aggregate", "markdown_table", "align", "match_innovations", "normalize"]
