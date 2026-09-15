"""测试用的合成论文 PDF。

为什么不用真论文做测试数据：真论文有版权问题，而且内容会变，没法写确定性断言。
这里手工造一篇**结构像论文**的合成论文（英文，避免 PDF 内置字体不支持中文的问题），
它包含 mock provider 会精确引用的句子，所以"引文核验"这条链路可以被确定性地测出来。

真论文的手动验证是另一回事：见 scripts/fetch_sample_paper.py。
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

# mock provider 会逐字引用这两句 —— 改这里必须同步改 devtools/mock_provider.py
# 注意：合成 PDF 的行宽有限（insert_text 超出页宽的部分不会被抽取出来），
# 引文行必须保持在大约 100 字符以内（KEY_QUOTE 100 字符可以完整抽出，
# 而 129 字符的旧 INIT_QUOTE 尾部被裁掉，导致"逐字引用"其实只能匹配到前 60 字符）。
KEY_QUOTE = (
    "Low-rank reparameterization reduces the number of trainable parameters "
    "by four orders of magnitude."
)
INIT_QUOTE = (
    "We initialize A with a random Gaussian distribution and B with zeros, "
    "so the update starts at zero."
)

_PAGES: list[list[str]] = [
    [  # 1
        "Papertest: A Synthetic Paper for Plumbing Verification",
        "A. Author, B. Author",
        "Institute of Synthetic Results",
        "",
        "Abstract",
        "We present Papertest, a deliberately small paper whose only purpose is to make",
        "an agent pipeline testable end to end. Our method introduces a low-rank",
        "reparameterization of weight updates and a matched initialization scheme.",
        "Experiments on synthetic benchmarks show the same accuracy with far fewer",
        "trainable parameters.",
    ],
    [  # 2
        "1 Introduction",
        "Adapting large models to downstream tasks is expensive because every task",
        "requires a full copy of the model parameters. Existing approaches either",
        "fine-tune all weights or prepend additional input tokens.",
        "In this paper we ask whether the update itself can be represented with far",
        "fewer numbers, and we answer affirmatively with a simple decomposition.",
    ],
    [  # 3
        "3 Method",
        "3.1 Background",
        "Let W0 be the pretrained weight matrix and dW the update obtained by training.",
        "3.2 Low-Rank Reparameterization",
        KEY_QUOTE,
        "Concretely we write dW = B A, where B has shape (d, r) and A has shape (r, k),",
        "and r is chosen to be much smaller than both d and k. During training W0 is",
        "frozen and only A and B receive gradients.",
    ],
    [  # 4
        "3.3 Initialization and Scaling",
        INIT_QUOTE,
        "The forward pass scales the low-rank branch by alpha / r, which keeps the",
        "magnitude of the update roughly constant when r changes.",
    ],
    [  # 5
        "4 Experiments",
        "We evaluate on three synthetic benchmarks. With r = 4 the method matches the",
        "accuracy of full fine-tuning while training less than one percent of the",
        "parameters. Table 1 reports the ablation over r.",
    ],
    [  # 6
        "5 Conclusion",
        "Low-rank reparameterization makes task adaptation cheap without changing the",
        "model architecture.",
        "References",
        "[1] A. Author. A very relevant prior work. 2021.",
        "[2] B. Author. Another very relevant prior work. 2022.",
    ],
]


def build_pdf(path: Path, *, pages: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for lines in _PAGES[: pages or len(_PAGES)]:
        page = doc.new_page()
        y = 72.0
        for line in lines:
            page.insert_text((72, y), line, fontsize=11)
            y += 16
    doc.save(path)
    doc.close()
    return path


def build_huge_pdf(path: Path, *, pages: int = 10, chars_per_page: int = 20_000) -> Path:
    """超大 PDF：用来测"单页截断"和"全文读取超限"这两条护栏。

    要点：用 insert_textbox + 极小字号铺满整页。
    逐行 insert_text 是不行的——排版到页面之外的行会被直接丢掉，凑不够字符数。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        f"line {index:05d} filler sentence for the huge fixture, used to exceed the budget. "
        for index in range(chars_per_page // 72 + 1)
    )[:chars_per_page]
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text((20, 30), f"page {index + 1}", fontsize=11)
        page.insert_textbox(pymupdf.Rect(20, 40, 575, 820), body, fontsize=3)
    doc.save(path)
    doc.close()
    return path


def fixture_path(root: Path) -> Path:
    return root / "tests" / "fixtures" / "synthetic_paper.pdf"


def huge_fixture_path(root: Path) -> Path:
    return root / "tests" / "fixtures" / "huge_paper.pdf"


def ensure_fixtures(root: Path) -> tuple[Path, Path]:
    small = fixture_path(root)
    huge = huge_fixture_path(root)
    if not small.exists():
        build_pdf(small)
    if not huge.exists():
        build_huge_pdf(huge)
    return small, huge


if __name__ == "__main__":  # pragma: no cover
    here = Path(__file__).resolve().parents[1]
    small, huge = ensure_fixtures(here)
    print(f"生成：{small}\n生成：{huge}")
