"""测试用的合成仓库。

为什么不用真仓库做测试数据：
- 真仓库会持续演进，行号和文件结构都会变，没法写确定性断言；
- 而且拿别人的仓库当测试夹具本身也不合适。

所以这里手工造一个小仓库，内容是**自洽的**：
它实现了"低秩重参数化"和"零初始化 + alpha/r 缩放"，但**没有**实现"冻结主干"。
所以 mock provider 说 inn-3「找不到」这件事，是**真的**——测试因此同时在验证 not_found 路径。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

LAYERS_PY = '''"""A tiny LoRA-style reparameterization, written for the PaperLens fixture."""

import math

import torch
import torch.nn as nn


class LoRALayer:
    """低秩分支的公共部分：r 是秩，lora_alpha / r 是前向缩放系数。"""

    def __init__(self, r: int, lora_alpha: int, lora_dropout: float = 0.0):
        self.r = r
        self.lora_alpha = lora_alpha
        self.lora_dropout = nn.Dropout(p=lora_dropout) if lora_dropout > 0.0 else (lambda x: x)
        self.scaling = lora_alpha / r
        self.merged = False

    def reset_parameters(self):
        # A 用高斯初始化，B 置零，于是训练开始时整个更新为零
        nn.init.kaiming_uniform_(self.lora_A.weight, a=math.sqrt(5))
        nn.init.zeros_(self.lora_B.weight)


class Linear(nn.Linear, LoRALayer):
    """把 nn.Linear 换成 dW = B A 的低秩形式。"""

    def __init__(self, in_features, out_features, r=0, lora_alpha=1, lora_dropout=0.0, **kwargs):
        nn.Linear.__init__(self, in_features, out_features, **kwargs)
        LoRALayer.__init__(self, r=r, lora_alpha=lora_alpha, lora_dropout=lora_dropout)
        if r > 0:
            self.lora_A = nn.Linear(in_features, r, bias=False)
            self.lora_B = nn.Linear(r, out_features, bias=False)
            self.scaling = lora_alpha / r
        self.reset_parameters()

    def forward(self, x):
        result = nn.Linear.forward(self, x)
        if self.r > 0 and not self.merged:
            after_A = self.lora_A(self.lora_dropout(x))
            after_B = self.lora_B(after_A)
            result += after_B * self.scaling
        return result
'''

TRAIN_PY = '''"""Plain training loop. Note: no parameter freezing here."""

import torch
from torch.optim import AdamW

from loralib.layers import Linear


def build_model(hidden=64, rank=4):
    return torch.nn.Sequential(Linear(hidden, hidden, r=rank, lora_alpha=rank * 2), torch.nn.ReLU())


def main(steps=10):
    model = build_model()
    optimizer = AdamW(model.parameters(), lr=1e-3)
    for step in range(steps):
        loss = model(torch.randn(8, 64)).pow(2).mean()
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        print(f"step {step} loss {loss.item():.4f}")


if __name__ == "__main__":
    main()
'''

README_MD = """# sample-repo

PaperLens 的测试仓库。它只用来说明"低秩重参数化"怎么落地，**没有**实现冻结主干。
"""

REQUIREMENTS = "torch>=2.0\n"

HELPERS_PY = '''"""Unrelated helpers, kept here so search has some noise to filter."""


def format_seconds(value: float) -> str:
    return f"{value:.1f}s"


def chunked(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]
'''

JUNK_JS = "// vendored dependency, must be skipped by the tools\nexport const junk = 1;\n"


def layer_lines(marker: str) -> tuple[int, int]:
    """返回 LAYERS_PY 中从 marker 开始到该代码块结束的行号（1-based，含）。

    让 mock provider 引用行号时**算出来的**，而不是写死的 —— 改夹具不会让测试静默失效。
    """
    lines = LAYERS_PY.split("\n")
    start = next(index for index, line in enumerate(lines) if marker in line)
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = start
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            end = index
            continue
        current = len(line) - len(line.lstrip())
        if current <= indent and line.strip():
            break
        end = index
    # 文件以换行结尾，split 出来的最后一项是空串，会把行号多算一行
    while end > start and not lines[end].strip():
        end -= 1
    return start + 1, end + 1


def quote_from_lines(start: int, end: int, needle: str) -> str:
    for line in LAYERS_PY.split("\n")[start - 1 : end]:
        if needle in line:
            return line.strip()
    raise AssertionError(f"夹具里找不到 {needle!r}")


LARGE_PY = "\n".join(
    f"TABLE_{index:03d} = [{index}, {index * 2}, {index * 3}]" for index in range(1, 2501)
) + "\n"


FILES: dict[str, str] = {
    "README.md": README_MD,
    "requirements.txt": REQUIREMENTS,
    "loralib/__init__.py": 'from .layers import Linear, LoRALayer\n\n__all__ = ["Linear", "LoRALayer"]\n',
    "loralib/layers.py": LAYERS_PY,
    "train.py": TRAIN_PY,
    "utils/helpers.py": HELPERS_PY,
    # 故意提交一个依赖目录：用来验证工具会跳过 node_modules
    "node_modules/junk/index.js": JUNK_JS,
    # 一个 2500 行的文件：用来验证"读整个文件会自动截断"（上限 2000 行）
    "utils/generated_tables.py": LARGE_PY,
}


def repo_path(root: Path) -> Path:
    return root / "tests" / "fixtures" / "sample_repo"


def _matches_fixture(path: Path) -> bool:
    """磁盘上的仓库和 FILES 定义是否一致。不一致就重建——
    否则改了夹具定义却复用了旧仓库，测试会静默测旧内容。"""
    for relative, content in FILES.items():
        target = path / relative
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            return False
    return True


def build_repo(root: Path, *, force: bool = False) -> Path:
    """建仓库并提交一次。提交时间/作者写死，让 commit 号在同样内容下保持一致。"""
    path = repo_path(root)
    if (path / ".git").exists() and not force and _matches_fixture(path):
        return path
    if path.exists():
        import shutil

        shutil.rmtree(path)
    for relative, content in FILES.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return commit_all(path)


def commit_all(path: Path) -> Path:
    """把 path 里的现有文件 git init + 提交一次（作者/时间写死，commit 可复现）。"""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "PaperLens Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "PaperLens Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    }
    for command in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "-A"],
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "initial fixture commit"],
    ):
        result = subprocess.run(command, cwd=path, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"{command} 失败：{result.stderr.strip()}")
    return path


def build_mini_repo(path: Path, files: dict[str, str]) -> Path:
    """按 {相对路径: 内容} 造一个一次性的小仓库（不走 build_repo 的自愈逻辑）。

    用来测「夹具仓库不方便承载」的边界情况，比如开头/结尾带连续空行的文件
    ——那种文件不能塞进 sample_repo，否则夹具 commit 变了，goldset 冻结的 commit 就对不上了。
    """
    if path.exists():
        import shutil

        shutil.rmtree(path)
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return commit_all(path)


if __name__ == "__main__":  # pragma: no cover
    here = Path(__file__).resolve().parents[1]
    created = build_repo(here)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=created, capture_output=True, text=True).stdout.strip()
    print(f"仓库：{created}\ncommit：{head}")
