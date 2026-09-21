"""从真实渲染的墨迹验证高亮位置，而不是重写坐标变换公式。"""
import tempfile
import unittest
from pathlib import Path

import pymupdf

from app.paper import PaperDocument


class PaperGeometryTest(unittest.TestCase):
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
