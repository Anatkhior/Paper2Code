"""从真实渲染的墨迹验证高亮位置，而不是重写坐标变换公式。"""
import tempfile
import unittest
from pathlib import Path

import pymupdf

from app.paper import PaperDocument


class PaperGeometryTest(unittest.TestCase):
    def locate(self, lines, quote, **kwargs):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "quote.pdf"
            with pymupdf.open() as doc:
                page = doc.new_page(width=800, height=800)
                for x, y, text in lines:
                    page.insert_text((x, y), text, fontsize=11)
                doc.save(path)
            paper = PaperDocument(path)
            try:
                return paper.quote_rects(1, quote, **kwargs)
            finally:
                paper.close()

    def test_two_columns_never_merge_into_one_wide_box(self):
        left = "Alpha beta gamma delta"
        right = "epsilon zeta eta theta"
        rects, coverage = self.locate([(50, 100, left), (450, 100, right)], f"{left} {right}")
        self.assertEqual(coverage, 1)
        self.assertEqual(len(rects), 2)
        self.assertTrue(all(x1 <= 400 or x0 >= 450 for x0, _, x1, _ in rects))

    def test_repeated_quote_highlights_one_occurrence(self):
        quote = "A uniquely identifiable sentence"
        rects, coverage = self.locate([(50, 100, quote), (450, 300, quote)], quote)
        self.assertEqual(coverage, 1)
        self.assertEqual(len(rects), 1)
        self.assertLess(rects[0][0], 400)

    def test_overlapping_windows_in_different_columns_cannot_complete_quote(self):
        words = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu".split()
        rects, coverage = self.locate([(40, 100, " ".join(words[:8])), (420, 300, " ".join(words[4:]))], " ".join(words))
        self.assertGreater(coverage, 0)
        self.assertLess(coverage, 1)
        self.assertEqual(len(rects), 1)

    def test_substrings_of_words_are_not_quote_matches(self):
        rects, coverage = self.locate([(50, 100, "training parameters updates faithfully")], "train parameter update faithful")
        self.assertEqual((rects, coverage), ([], 0))

    def test_missing_formula_does_not_highlight_unmatched_words(self):
        lines = [(50, 100, "Alpha beta gamma delta OMITTED epsilon zeta eta theta")]
        quote = "Alpha beta gamma delta (x+y) epsilon zeta eta theta"
        rects, coverage = self.locate(lines, quote)
        self.assertGreater(coverage, 0.5)
        self.assertLess(coverage, 1)
        self.assertEqual(len(rects), 2)
        # The two matched spans must leave the intervening, unquoted word untouched.
        self.assertGreater(rects[1][0] - rects[0][2], 40)

    def test_hyphenated_multiline_quote_and_rectangle_limit(self):
        lines = [(50, 100, "Low rank trainable parame-"), (50, 116, "ters reduce memory requirements.")]
        quote = "Low rank trainable parameters reduce memory requirements."
        rects, coverage = self.locate(lines, quote)
        self.assertEqual(coverage, 1)
        self.assertEqual(len(rects), 2)
        limited, partial = self.locate(lines, quote, max_rects=1)
        self.assertEqual(len(limited), 1)
        self.assertGreater(partial, 0)
        self.assertLess(partial, 1)

    def test_unrelated_common_words_do_not_create_highlights(self):
        self.assertEqual(self.locate([(50, 100, "The paper describes a method for training parameters.")], "definitely not on this page at all"), ([], 0))

    def test_normalized_fraction_cannot_match_only_its_numerator(self):
        prefix = "Alpha beta gamma delta"
        lines = [(50, 100, prefix + " ½")]
        rects, coverage = self.locate(lines, prefix + " 1")
        self.assertAlmostEqual(coverage, 19 / 20)
        self.assertEqual(len(rects), 1)
        self.assertAlmostEqual(rects[0][2], 50 + pymupdf.get_text_length(prefix, fontsize=11), places=3)
        _, complete = self.locate(lines, prefix + " ½")
        self.assertEqual(complete, 1)

    def test_rotated_and_cropped_highlights_cover_rendered_ink(self):
        quote = "Rotation crop geometry fixture"
        with tempfile.TemporaryDirectory() as folder:
            for crop in (False, True):
                for angle in (0, 90, 180, 270):
                    with self.subTest(crop=crop, rotation=angle):
                        path = Path(folder) / f"{crop}-{angle}.pdf"
                        with pymupdf.open() as doc:
                            page = doc.new_page(width=600, height=800)
                            page.insert_text((170, 250), quote, fontsize=18)
                            if crop:
                                page.set_cropbox(pymupdf.Rect(60, 100, 550, 720))
                            page.set_rotation(angle)
                            doc.save(path)
                        paper = PaperDocument(path)
                        try:
                            rects, coverage = paper.quote_rects(1, quote)
                            self.assertEqual(coverage, 1)
                            self.assertEqual(len(rects), 1)
                            image, _ = paper.page_image(1, dpi=72)
                            pix = pymupdf.Pixmap(image)
                            self.assertEqual(paper.page_box(1), (pix.width, pix.height))
                            pixels = pix.samples
                            ink = [(i % pix.width, i // pix.width) for i in range(pix.width * pix.height)
                                   if min(pixels[i * pix.n:i * pix.n + 3]) < 180]
                            self.assertTrue(ink)
                            xs, ys = zip(*ink)
                            bounds = (min(xs), min(ys), max(xs) + 1, max(ys) + 1)
                            x0, y0, x1, y1 = rects[0]
                            self.assertLessEqual(x0, bounds[0] + 1)
                            self.assertLessEqual(y0, bounds[1] + 1)
                            self.assertGreaterEqual(x1, bounds[2] - 1)
                            self.assertGreaterEqual(y1, bounds[3] - 1)
                            # 字体搜索框包含 ascent/descent，允许 18pt 字号内的小余量。
                            self.assertTrue(all(abs(a - b) < 9 for a, b in zip(rects[0], bounds)))
                        finally:
                            paper.close()


if __name__ == "__main__":
    unittest.main()
