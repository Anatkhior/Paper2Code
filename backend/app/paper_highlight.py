"""Locate one occurrence of a quote and keep its glyph provenance through normalization."""

from collections import Counter
from dataclasses import dataclass
from difflib import Match, SequenceMatcher
import re
import unicodedata

import pymupdf


_TOKENS = re.compile(r"\w+|[^\w\s]")
_HYPHEN_BREAK = re.compile(r"[A-Za-z]-\n[a-z]")
MIN_ANCHOR_WORDS = 4
MAX_FORMULA_GAP_TOKENS = 12
MIN_PARTIAL_COVERAGE = 0.5


@dataclass(frozen=True, slots=True)
class Glyph:
    text: str
    rect: tuple[float, float, float, float]
    line: tuple[int, int]


@dataclass(frozen=True, slots=True)
class Token:
    text: str
    start: int
    end: int


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().replace("\u00ad", "")


def _index_page(page) -> tuple[list[Token], list[Glyph], Counter]:
    glyphs = []
    for block_id, block in enumerate(page.get_text("rawdict")["blocks"]):
        for line_id, line in enumerate(block.get("lines", [])):
            for span in line["spans"]:
                glyphs.extend(Glyph(char["c"], tuple(char["bbox"]), (block_id, line_id)) for char in span["chars"])
            glyphs.append(Glyph("\n", (0, 0, 0, 0), (block_id, line_id)))
    raw = "".join(glyph.text for glyph in glyphs)
    # PDF line-break hyphens disappear from matching, but their boxes remain in the source span.
    skipped = {index for match in _HYPHEN_BREAK.finditer(raw) for index in range(match.start() + 1, match.end() - 1)}
    normalized, source = [], []
    for index, glyph in enumerate(glyphs):
        value = "" if index in skipped else _normalize(glyph.text)
        normalized.extend(value)
        source.extend([index] * len(value))
    text = "".join(normalized)
    tokens = [Token(match.group(), source[match.start()], source[match.end() - 1] + 1) for match in _TOKENS.finditer(text)]
    weights = Counter(source[index] for index, char in enumerate(text) if not char.isspace())
    return tokens, glyphs, weights


def _complete_glyph_match(match: Match, tokens: list[Token]) -> Match:
    start, end = match.b, match.b + match.size
    # NFKC can expand one printed glyph into several tokens (½ → 1 ⁄ 2).
    # A box cannot highlight only part of that glyph: drop incomplete edge matches.
    while start < end and start > 0 and tokens[start - 1].end > tokens[start].start:
        start += 1
    while end > start and end < len(tokens) and tokens[end - 1].end > tokens[end].start:
        end -= 1
    return Match(match.a + start - match.b, start, end - start)


def _matching_blocks(target: list[str], tokens: list[Token], glyphs: list[Glyph]):
    matcher = SequenceMatcher(None, target, [token.text for token in tokens], autojunk=False)
    blocks = [_complete_glyph_match(block, tokens) for block in matcher.get_matching_blocks() if block.size]
    blocks = [block for block in blocks if block.size]
    if not blocks:
        return []
    anchor = max(blocks, key=lambda block: block.size)
    if anchor.size == len(target):
        return [anchor]
    # Partial matching requires a meaningful contiguous anchor, never scattered common words.
    words = target[anchor.a:anchor.a + anchor.size]
    if sum(any(char.isalnum() for char in word) for word in words) < MIN_ANCHOR_WORDS:
        return []
    anchor_index = blocks.index(anchor)
    chosen = [anchor]
    anchor_block = glyphs[tokens[anchor.b].start].line[0]

    def adjacent(left, right):
        quote_gap = right.a - left.a - left.size
        page_gap = right.b - left.b - left.size
        # Only tolerate a small damaged/formula region in the quote. Do not skip extra
        # prose on the page or bridge separate text blocks to manufacture a full match.
        return (
            0 < quote_gap <= MAX_FORMULA_GAP_TOKENS
            and page_gap <= MAX_FORMULA_GAP_TOKENS
            and glyphs[tokens[right.b + right.size - 1].end - 1].line[0] == anchor_block
            and glyphs[tokens[left.b].start].line[0] == anchor_block
        )

    for block in reversed(blocks[:anchor_index]):
        if not adjacent(block, chosen[0]):
            break
        chosen.insert(0, block)
    for block in blocks[anchor_index + 1:]:
        if not adjacent(chosen[-1], block):
            break
        chosen.append(block)
    return chosen


def locate_quote(page, quote: str, *, max_rects: int) -> tuple[list[tuple], float]:
    """Return only matched glyphs from one occurrence; coverage counts normalized characters.

    For damaged text/formulas, keep the strongest contiguous anchor and nearby matches
    within its text block. Uncertain portions stay unhighlighted with partial coverage.
    """
    target = _TOKENS.findall(_normalize(_HYPHEN_BREAK.sub(lambda match: match[0][0] + match[0][-1], quote)))
    if not target:
        return [], 0.0
    tokens, glyphs, weights = _index_page(page)
    matches = _matching_blocks(target, tokens, glyphs)
    total = sum(map(len, target))
    matched = sum(len(token.text) for match in matches for token in tokens[match.b:match.b + match.size])
    if matched / total < MIN_PARTIAL_COVERAGE:
        return [], 0.0

    segments = []
    for match in matches:
        current = None
        line = None
        weight = 0
        for index in range(tokens[match.b].start, tokens[match.b + match.size - 1].end):
            glyph = glyphs[index]
            if glyph.text.isspace():
                continue
            rect = pymupdf.Rect(glyph.rect)
            # Never union across lines, columns, or an unmatched region of the quote.
            if current is not None and (glyph.line != line or rect.x0 - current.x1 > max(2, rect.height * 0.5)):
                segments.append((tuple(current), weight))
                current, weight = None, 0
            current = rect if current is None else current | rect
            line = glyph.line
            weight += weights[index]
        if current is not None:
            segments.append((tuple(current), weight))
    visible = segments[:max_rects]
    coverage = sum(weight for _, weight in visible) / total
    return [rect for rect, _ in visible], round(min(1.0, coverage), 3)
