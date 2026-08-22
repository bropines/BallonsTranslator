import os
import unittest
import numpy as np

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from qtpy.QtWidgets import QApplication
from qtpy.QtCore import QRectF

from ballontranslator.utils.fontformat import FontFormat
from ballontranslator.utils.textblock import TextBlock
from ballontranslator.ui.text_engine.horizontal_layout import compute_polygon_scanline_span


class PolygonToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_fontformat_polygon_model(self):
        pts = [[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0]]
        fmt = FontFormat(shape_type='polygon', polygon_points=pts)
        self.assertEqual(fmt.shape_type, 'polygon')
        self.assertEqual(len(fmt.polygon_points), 4)

        # Invalid points fallback
        fmt_invalid = FontFormat(shape_type='polygon', polygon_points=[[0, 0]])
        self.assertIsNone(fmt_invalid.polygon_points)

    def test_textblock_polygon_properties_and_mask(self):
        blk = TextBlock([10, 20, 110, 120])
        blk.shape_type = 'polygon'
        pts = [[10.0, 20.0], [110.0, 20.0], [110.0, 120.0], [10.0, 120.0]]
        blk.polygon_points = pts
        self.assertEqual(blk.shape_type, 'polygon')
        self.assertEqual(len(blk.polygon_points), 4)

        # Mask generation
        mask = blk.get_polygon_mask()
        self.assertIsNotNone(mask)
        self.assertEqual(mask.shape, (100, 100))
        self.assertEqual(mask[50, 50], 255)

    def test_polygon_scanline_span(self):
        # Triangle polygon with base [0, 100] to [100, 100] and top at [50, 0]
        triangle_pts = [[50.0, 0.0], [100.0, 100.0], [0.0, 100.0]]
        
        # Scanline at y = 50 (midpoint) should have width ~50
        span_x, span_w = compute_polygon_scanline_span(triangle_pts, 50.0, 100.0, 100.0)
        self.assertAlmostEqual(span_w, 50.0, delta=1.0)
        self.assertAlmostEqual(span_x, 25.0, delta=1.0)

        # Scanline at top y = 10 should be narrow
        span_x_top, span_w_top = compute_polygon_scanline_span(triangle_pts, 10.0, 100.0, 100.0)
        self.assertLess(span_w_top, span_w)


if __name__ == '__main__':
    unittest.main()
