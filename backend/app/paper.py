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


def _merge_rects(rects: list[tuple[float, float, float, float]]) -> list[tuple[float, float, float, float]]:
    """把同一行上的碎矩形并成一段（逐词匹配会产生很多小框，画出来很碎）。

    判定"同一行"：纵向重叠超过一半。合并后按读序排序、去重。
    """
    if not rects:
        return []
    ordered = sorted(rects, key=lambda item: (round(item[1], 1), item[0]))
    merged: list[list[float]] = []
    for x0, y0, x1, y1 in ordered:
        target = None
        for item in merged:
            overlap = min(item[3], y1) - max(item[1], y0)
            shorter = min(item[3] - item[1], y1 - y0)
            if shorter > 0 and overlap / shorter > 0.5:
                target = item
                break
        if target is None:
            merged.append([x0, y0, x1, y1])
        else:
            target[0] = min(target[0], x0)
            target[1] = min(target[1], y0)
            target[2] = max(target[2], x1)
            target[3] = max(target[3], y1)
    seen: set[tuple[int, int, int, int]] = set()
    unique: list[tuple[float, float, float, float]] = []
    for x0, y0, x1, y1 in merged:
        key = (round(x0), round(y0), round(x1), round(y1))
        if key in seen:
            continue
        seen.add(key)
        unique.append((x0, y0, x1, y1))
    return unique


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

    # -- 原版页面：渲染图 + 高亮矩形（给前端"PDF 视图也高亮"用，2026-09-16）-------
    def _image_path(self, page: int, dpi: int) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / "pages" / f"p{page:04d}@{dpi}.png"

    def page_image(self, page: int, dpi: int = 150) -> tuple[bytes, str]:
        """把某一页渲染成 PNG（带磁盘缓存）。返回 (字节, media_type)。

        为什么前端不再直接内嵌浏览器 PDF 阅读器：内置阅读器**不允许外部脚本操作它内部的 DOM**，
        所以"在 PDF 原版里高亮同一段"没法用 iframe 做到（`#search=` 在 Chrome 上并不生效，
        2026-09-16 用户实测）。改成"服务端渲染该页 + 按页码坐标叠高亮框"：
        任何浏览器行为一致，而且用的是核验引文的同一个库（PyMuPDF），高亮位置和核验口径一致。
        """
        if not 1 <= page <= self.page_count:
            raise ValueError(f"页码 {page} 超出范围（这篇论文共 {self.page_count} 页）")
        cached = self._image_path(page, dpi)
        if cached and cached.exists():
            return cached.read_bytes(), "image/png"
        pixmap = self._open()[page - 1].get_pixmap(dpi=dpi)
        data = pixmap.tobytes("png")
        if cached:
            cached.write_bytes(data)
        return data, "image/png"

    def page_box(self, page: int) -> tuple[float, float]:
        """页面尺寸（PDF 点，1/72 英寸）。前端按它把高亮矩形换算成百分比。"""
        if not 1 <= page <= self.page_count:
            raise ValueError(f"页码 {page} 超出范围（这篇论文共 {self.page_count} 页）")
        rect = self._open()[page - 1].rect
        return float(rect.width), float(rect.height)

    # -- 引文定位（几何序列匹配；字符串匹配只当快路径）-------------------------
    @staticmethod
    def _pick_after(candidates: list, cursor) -> object | None:
        """在候选里挑一个"读序上紧跟在 cursor 之后"的矩形。

        同一行看 x，允许跨行（行距最多按 3 行算），但距离不能太远——否则会跳到页面上
        另一个无关的位置上去。
        """
        best = None
        best_key = None
        for rect in candidates:
            same_line = abs(rect.y0 - cursor.y0) < 4
            if same_line:
                if rect.x0 < cursor.x1 - 1:
                    continue
                key = (0, rect.x0 - cursor.x1)
            else:
                gap = rect.y0 - cursor.y0
                if not (0 < gap < 60):
                    continue
                key = (1, gap, rect.x0)
            if best_key is None or key < best_key:
                best_key, best = key, rect
        return best

    @classmethod
    def _pick_before(cls, candidates: list, cursor) -> object | None:
        """反方向：读序上紧挨在 cursor 之前。"""
        best = None
        best_key = None
        for rect in candidates:
            same_line = abs(rect.y0 - cursor.y0) < 4
            if same_line:
                if rect.x1 > cursor.x0 + 1:
                    continue
                key = (0, cursor.x0 - rect.x1)
            else:
                gap = cursor.y0 - rect.y0
                if not (0 < gap < 60):
                    continue
                key = (1, gap, -rect.x0)
            if best_key is None or key < best_key:
                best_key, best = key, rect
        return best

    def _geometry_hits(self, pdf_page, words: list[str]) -> tuple[list[tuple[float, float, float, float]], float]:
        """**按词的几何序列**在页面里拼出引文位置。

        为什么需要它（2026-09-17 用户实测：跨行的引文只高亮了前半截）：
        字符串匹配（`search_for(整段)` 或滑窗）对空白极其敏感——真论文里有
        连字符折行、非 ASCII 空格、两端对齐拉伸的空格、双栏换列……任何一种都会让
        "整段/整窗"匹配失败，于是只亮出一部分。
        这里改成：逐词找位置，再按**读序**（同一行看 x、换行看行距）把它们串起来；
        某个词找不到（例如公式）就跳过它继续，不因此中断整句。
        覆盖率 = 找到的词数 / 总词数。
        """
        hits: list[list] = []
        for word in words:
            candidates = [word]
            stripped = word.strip('.,;:()[]{}\"\'“”‘’')
            if stripped and stripped != word and len(stripped) >= 2:
                candidates.append(stripped)
            # 连字符折行会把词切成两半（"parame-" / "ters"），整词搜不到 →
            # 再试一次"词的前半段"，几何约束会保证它仍落在正确的读序位置上
            if len(word) >= 6:
                candidates.append(word[: max(4, len(word) // 2)])
            found: list = []
            for candidate in candidates:
                if len(candidate) < 2:
                    continue
                found = pdf_page.search_for(candidate, flags=pymupdf.TEXT_DEHYPHENATE) or []
                if found:
                    break
            hits.append(list(found))

        anchors = [index for index, item in enumerate(hits) if item]
        if not anchors:
            return [], 0.0
        # 锚点挑"最长且候选最少"的词：最能定位到唯一位置
        anchor = max(anchors, key=lambda index: (len(words[index]), -len(hits[index])))

        best_chosen: dict[int, object] = {}
        for anchor_rect in hits[anchor]:
            chosen: dict[int, object] = {anchor: anchor_rect}
            cursor = anchor_rect
            for index in range(anchor + 1, len(words)):
                nxt = self._pick_after(hits[index], cursor)
                if nxt is not None:
                    chosen[index] = nxt
                    cursor = nxt
            cursor = anchor_rect
            for index in range(anchor - 1, -1, -1):
                prev = self._pick_before(hits[index], cursor)
                if prev is not None:
                    chosen[index] = prev
                    cursor = prev
            if len(chosen) > len(best_chosen):
                best_chosen = chosen

        if not best_chosen:
            return [], 0.0

        # 防误报：引文里全是常见词时（"definitely not in this paper" 这种），
        # 逐词匹配能把页面上零散的同名词串起来 → 画出一堆假框。
        # 所以要求**既要有足够覆盖率，又要有一段足够长的连续命中**。
        ordered = sorted(best_chosen)
        longest_run = 1
        current_run = 1
        for previous, current in zip(ordered, ordered[1:]):
            current_run = current_run + 1 if current == previous + 1 else 1
            longest_run = max(longest_run, current_run)
        coverage = len(best_chosen) / len(words)
        if coverage < 0.5 or longest_run < 4:
            return [], 0.0

        rects = [best_chosen[index] for index in ordered]
        return _merge_rects(rects), round(min(1.0, coverage), 3)

    def quote_rects(
        self, page: int, quote: str, *, max_rects: int = 40
    ) -> tuple[list[tuple[float, float, float, float]], float]:
        """在某一页里找出引文的位置（PDF 点坐标），并给出**覆盖率**。

        返回 (矩形列表, 覆盖率)；覆盖率 = 匹配到的词数 / 引文总词数。

        两级策略：
          ① 快路径——字符串匹配（整段 → 8 词滑窗）覆盖大多数规整引文；
          ② 兜底——**按词的几何序列**匹配（见 `_geometry_hits`），
             专治连字符折行、非 ASCII 空格、双栏换列、公式夹杂这类"整段串不起来"的情况。
        取覆盖率更高的那一份；覆盖率 <1 时前端会明说"只覆盖约 N%"。
        """
        if not 1 <= page <= self.page_count:
            raise ValueError(f"页码 {page} 超出范围（这篇论文共 {self.page_count} 页）")
        # 注意：这里**不能**用 _squash（那是给"比较引文"用的，会把空白全删掉，
        # "Low-rank reparameterization…" 变成一整串就再也搜不到了）。
        target = " ".join(quote.split())
        if not target:
            return [], 0.0
        pdf_page = self._open()[page - 1]
        words = target.split(" ")
        total_words = len(words)

        def _hits(text: str, flags: int = 0) -> list[tuple[float, float, float, float]]:
            return [
                (float(hit.x0), float(hit.y0), float(hit.x1), float(hit.y1))
                for hit in pdf_page.search_for(text, flags=flags)
            ]

        # ① 整段（允许跨连字符折行）
        whole = _hits(target, pymupdf.TEXT_DEHYPHENATE)
        if whole:
            return _merge_rects(whole)[:max_rects], 1.0

        # ② 滑窗（8 词窗口 / 步长 4）：命中的窗口并起来
        window, step = 8, 4
        matched: list[tuple[float, float, float, float]] = []
        matched_indices: set[int] = set()   # 按词下标记账：滑窗有重叠，累加长度会把覆盖率算爆
        index = 0
        while index < total_words:
            chunk_words = words[index : index + window]
            if len(chunk_words) < 3 and index > 0:
                break
            found = _hits(" ".join(chunk_words))
            if found:
                matched.extend(found)
                matched_indices.update(range(index, index + len(chunk_words)))
            if index + window >= total_words:
                break
            index += step
        string_best = (matched, len(matched_indices) / total_words if total_words else 0.0)

        # ③ 几何兜底：字符串法没吃满（覆盖率 <1）的时候再试，谁覆盖得多用谁
        if string_best[1] >= 0.999:
            return _merge_rects(matched)[:max_rects], 1.0
        geometry_rects, geometry_coverage = self._geometry_hits(pdf_page, words)
        if geometry_coverage > string_best[1]:
            return geometry_rects[:max_rects], geometry_coverage
        return _merge_rects(matched)[:max_rects], round(string_best[1], 3)

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
