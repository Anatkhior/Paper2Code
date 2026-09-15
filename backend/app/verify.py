"""确定性重放校验（docs/v0-spec.md §7）。

这是整个项目的**头号指标**所在：
    citation_verifiable_rate = 通过核验的引用数 / 全部引用数

为什么它比"解读读起来准不准"更重要：
- 它是确定性的、可重复的、不花钱的 —— 换个模型、换个 prompt，这个数字照样可比；
- 它把"幻觉"从**不可知**变成**可计数**；
- 它不依赖任何模型判断，所以不存在"用模型验证模型"的自欺。

核验分四步（全部针对**代码**引用；论文引文在 record_plan 里由 quote_found 核验）：
1. commit 在仓库里存在吗
2. 这个 commit 上这个 path 存在吗
3. 行区间合法吗
4. 该行区间内容的归一化哈希，和记录下来的 snippet_sha256 一致吗（以及 quote 是否真的在其中）
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from .repo_source import RepoError, RepoSource, normalize_lines, text_lines


def snippet_sha256(text: str) -> str:
    return hashlib.sha256(normalize_lines(text).encode("utf-8")).hexdigest()


def _squash(text: str) -> str:
    return "".join(text.split())


def check_evidence(repo: RepoSource, evidence: dict[str, Any]) -> dict[str, Any]:
    """核验一条代码引用。返回 {state, failures, snippet_sha256, line_count}。

    注意：这个函数**只读 git 对象**（content_at_commit），不读工作区文件，
    所以"重放"是可信的——就算有人事后改了工作区，也骗不过去。
    """
    failures: list[str] = []
    path = str(evidence.get("path") or "")
    start = evidence.get("line_start")
    end = evidence.get("line_end")
    expected_hash = evidence.get("snippet_sha256")
    quote = evidence.get("quote")

    if not path:
        return {"state": "failed", "failures": ["缺少 path"], "snippet_sha256": None, "line_count": None}
    if not isinstance(start, int) or not isinstance(end, int):
        return {"state": "failed", "failures": ["line_start/line_end 必须是整数"], "snippet_sha256": None, "line_count": None}

    try:
        content = repo.content_at_commit(path)
    except RepoError as exc:
        return {"state": "failed", "failures": [str(exc)], "snippet_sha256": None, "line_count": None}

    # 行号必须和 read_file（Agent 看到的）数出来的一致，否则 Agent 照着工具返回的
    # 行号提交引用会被误判为非法——见 repo_source.text_lines 的注释。
    # （哈希仍然走 normalize_lines：两边在提交和重放时用同一套归一化，自洽即可。）
    lines = text_lines(content)
    total = len(lines)
    if start < 1 or end < start or end > total:
        failures.append(f"行区间 {start}-{end} 非法（该文件在这个 commit 上共 {total} 行）")
        return {"state": "failed", "failures": failures, "snippet_sha256": None, "line_count": total}

    snippet = "\n".join(lines[start - 1 : end])
    digest = snippet_sha256(snippet)

    if expected_hash and expected_hash != digest:
        failures.append("片段哈希不匹配：这段内容和你提交时不一样了（行号可能被改过）")
    if quote and _squash(quote) not in _squash(snippet):
        failures.append("你引用的片段没有出现在这段行里")

    return {
        "state": "failed" if failures else "verified",
        "failures": failures,
        "snippet_sha256": digest,
        "line_count": total,
    }


def verify_artifact(repo: RepoSource, artifact: dict[str, Any]) -> dict[str, Any]:
    """对整份产物重放核验，并把结果写回每一条 code_evidence.verification。

    返回的统计里 citation_verifiable_rate 就是要写进 README 的那个数字。
    """
    checked_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    verified = 0
    failed = 0
    total = 0
    details: list[dict[str, Any]] = []

    for innovation in artifact.get("innovations", []):
        for evidence in innovation.get("code_evidence", []):
            total += 1
            outcome = check_evidence(repo, evidence)
            evidence["verification"] = {
                "state": outcome["state"],
                "checked_at": checked_at,
                "failures": outcome["failures"],
                "snippet_sha256": outcome["snippet_sha256"],
                "file_lines": outcome["line_count"],
            }
            if outcome["state"] == "verified":
                verified += 1
            else:
                failed += 1
                details.append(
                    {
                        "innovation_id": innovation.get("id"),
                        "path": evidence.get("path"),
                        "line_start": evidence.get("line_start"),
                        "failures": outcome["failures"],
                    }
                )

    rate = (verified / total) if total else 0.0
    return {
        "commit_sha": repo.commit_sha,
        "checked_at": checked_at,
        "citations_total": total,
        "citations_verified": verified,
        "citations_failed": failed,
        "citation_verifiable_rate": round(rate, 4),
        "failures": details,
    }


def summarize(artifact: dict[str, Any], verification: dict[str, Any]) -> dict[str, Any]:
    """给前端/README 用的一段摘要。"""
    innovations = artifact.get("innovations", [])
    statuses: dict[str, int] = {}
    for innovation in innovations:
        status = str(innovation.get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "innovations": len(innovations),
        "status_counts": statuses,
        **verification,
    }
