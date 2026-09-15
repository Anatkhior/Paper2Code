"""PDF 读取层。

设计原则（docs/v0-spec.md 原则 1 与 §0 决策）：
后端只负责"把第 N 页的文本取出来"这一件事——不做分节、不做 AST、不做公式解析。
**哪一页重要、要读多少、什么时候停，是 Agent 的决定。**

唯一的例外是 normalize() 里那几行**极轻量**的清洗（软连字符、断行连字符、空白归一）：
它们属于"把抽取结果变成可读文本"，不属于"理解论文"。而且它们是**可核验性**的前提——
Agent 引用原文时用的就是我们这里返回的文本，两边必须一致。

额外的一个诚实性机制：quote_found()。
Agent 声称"论文第 3 页说了 X"时，这句话可以被机械核验（判断它是否真的出现在那一页），
和代码引用的 verify.py 是同一个思路。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

# 断行连字符：re-\nparameterization -> reparameterization
_HYPHEN_BREAK = re.compile(r"([A-Za-z])-\n([a-z])")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_MULTI_NEWLINE = re.compile(r"\n{3,}")


def normalize(raw: str) -> str:
    text = raw.replace("\u00ad", "")  # 软连字符
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    text = _MULTI_SPACE.sub(" ", text)
    text = _MULTI_NEWLINE.sub("\n\n", text)
    return text.strip()


def _squash(text: str) -> str:
    """比较引文时用：把一切空白折叠掉，避免因为换行/空格差异误判。"""
    return re.sub(r"\s+", "", text)


@dataclass(slots=True)
class PageStats:
    page: int
    chars: int
    first_line: str


class PaperDocument:
    """按需读取的 PDF。文本按页缓存到磁盘，所以 Agent 反复读同一页不会重复解析。"""

    def __init__(self, pdf_path: Path, cache_dir: Path | None = None) -> None:
        self.path = Path(pdf_path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        # 缓存目录按 PDF 内容哈希分命名空间：
        # 否则"同一路径换了文件"会命中上一份文件的旧缓存，静默返回错的正文。
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            digest = hashlib.sha256(self.path.read_bytes()).hexdigest()[:12]
            self.cache_dir = self.cache_dir / f"paper-{digest}"
            (self.cache_dir / "pages").mkdir(parents=True, exist_ok=True)
        self._doc: pymupdf.Document | None = None
        doc = self._open()
        self.page_count = doc.page_count
        meta = doc.metadata or {}
        self.title_guess = (meta.get("title") or "").strip()

    # -- 生命周期 -----------------------------------------------------------
    def _open(self) -> pymupdf.Document:
        if self._doc is None:
            self._doc = pymupdf.open(self.path)
        return self._doc

    def close(self) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None

    # -- 读取 ---------------------------------------------------------------
    def _cache_path(self, page: int) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / "pages" / f"p{page:04d}.txt"

    def page_text(self, page: int) -> str:
        """1-based 页码。超范围直接报错（由工具层翻译成人话）。"""
        if not 1 <= page <= self.page_count:
            raise ValueError(f"页码 {page} 超出范围（这篇论文共 {self.page_count} 页）")
        cached = self._cache_path(page)
        if cached and cached.exists():
            return cached.read_text(encoding="utf-8")
        text = normalize(self._open()[page - 1].get_text("text"))
        if cached:
            cached.write_text(text, encoding="utf-8")
        return text

    def page_stats(self) -> list[PageStats]:
        stats: list[PageStats] = []
        for page in range(1, self.page_count + 1):
            text = self.page_text(page)
            first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
            stats.append(PageStats(page=page, chars=len(text), first_line=first_line[:70]))
        return stats

    def search(self, query: str, max_hits: int = 10, context: int = 120) -> list[dict]:
        """朴素的子串搜索（大小写不敏感）。**不做语义检索**——那是 Agent 的活。"""
        needle = _squash(query).lower()
        if not needle:
            return []
        hits: list[dict] = []
        for page in range(1, self.page_count + 1):
            text = self.page_text(page)
            haystack = text.lower()
            start = 0
            while len(hits) < max_hits:
                index = haystack.find(query.lower(), start)
                if index < 0:
                    break
                hits.append(
                    {
                        "page": page,
                        "snippet": text[max(0, index - context) : index + len(query) + context].strip(),
                    }
                )
                start = index + max(1, len(query))
            if len(hits) >= max_hits:
                break
        if not hits:
            # 退一步：忽略空白后再找一次（PDF 抽出来的文本常常在奇怪的地方换行）
            for page in range(1, self.page_count + 1):
                squashed = _squash(self.page_text(page))
                if needle in squashed.lower():
                    hits.append({"page": page, "snippet": "（命中处跨行，请直接读这一页）"})
                if len(hits) >= max_hits:
                    break
        return hits

    def full_text(self) -> str:
        return "\n\n".join(
            f"=== 第 {page} 页 ===\n{self.page_text(page)}" for page in range(1, self.page_count + 1)
        )

    # -- 引文核验 -----------------------------------------------------------
    def quote_match(self, page: int, quote: str) -> str:
        """这句话在第 page 页匹配到什么程度：full（整句都在）/ partial（只有开头匹配）/ none。

        full 和 partial 都算「通过核验」，但 partial 必须被如实标出来：
        允许模型「轻微抄错」的同时，不能让「真开头 + 编造后半段」的引文
        在界面上看起来和逐字引用一模一样。
        """
        if not quote or not quote.strip():
            return "none"
        if not 1 <= page <= self.page_count:
            return "none"
        needle = _squash(quote).lower()
        if len(needle) < 8:
            return "none"  # 太短的片段没有核验价值
        squashed_page = _squash(self.page_text(page)).lower()
        if needle in squashed_page:
            return "full"
        # 容忍轻微抄错：前 60 个（折叠空白后的）字符对得上就算找到，
        # 但调用方拿到的分级是 partial，界面会显示「部分匹配」而不是「已核验」。
        if needle[:60] in squashed_page:
            return "partial"
        return "none"

    def quote_found(self, page: int, quote: str) -> bool:
        """这句话真的出现在第 page 页吗？（忽略空白差异，partial 也算找到）

        这是"论文证据"的机械核验。它防的是模型编造原文引用。
        需要区分「逐字匹配」和「只匹配到开头」时，请用 quote_match()。
        """
        return self.quote_match(page, quote) != "none"


__all__ = ["PaperDocument", "PageStats", "normalize"]
