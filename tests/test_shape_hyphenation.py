import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from qtpy.QtWidgets import QApplication

from ballontranslator.ui.text_engine.item import TextBlkItem
from ballontranslator.utils.fontformat import FontFormat
from ballontranslator.utils.textblock import TextBlock
from ballontranslator.utils.hyphenation import (
    hyphenate_text,
    hyphenate_word,
    strip_soft_hyphens,
)


class ShapeAndHyphenationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_fontformat_defaults(self):
        fmt = FontFormat()
        self.assertEqual(fmt.shape_type, 'rect')
        self.assertFalse(fmt.auto_hyphenate)

        # Permissive sanitization
        fmt_invalid = FontFormat(shape_type='unknown_shape', auto_hyphenate=None)
        self.assertEqual(fmt_invalid.shape_type, 'rect')
        self.assertFalse(fmt_invalid.auto_hyphenate)

    def test_textblock_properties(self):
        blk = TextBlock([0, 0, 100, 100])
        self.assertEqual(blk.shape_type, 'rect')
        self.assertFalse(blk.auto_hyphenate)

        blk.shape_type = 'ellipse'
        blk.auto_hyphenate = True
        self.assertEqual(blk.fontformat.shape_type, 'ellipse')
        self.assertTrue(blk.fontformat.auto_hyphenate)

    def test_hyphenation_functions(self):
        # Russian word hyphenation
        ru_text = "Трансляция баблов"
        ru_hyphenated = hyphenate_text(ru_text, lang='ru')
        self.assertIn('\u00ad', ru_hyphenated)
        self.assertEqual(strip_soft_hyphens(ru_hyphenated), ru_text)

        # English word hyphenation
        en_text = "Translation"
        en_hyphenated = hyphenate_text(en_text, lang='en')
        self.assertIn('\u00ad', en_hyphenated)
        self.assertEqual(strip_soft_hyphens(en_hyphenated), en_text)

        # HTML tag preservation
        html_input = "<b>Трансляция</b> <i>баблов</i>"
        html_output = hyphenate_text(html_input, lang='ru')
        self.assertTrue(html_output.startswith("<b>"))
        self.assertTrue(html_output.endswith("</i>"))
        self.assertEqual(strip_soft_hyphens(html_output), html_input)

    def test_item_shape_and_hyphenation_lifecycle(self):
        bounds = [0, 0, 200, 200]
        block = TextBlock(bounds)
        block._bounding_rect = list(bounds)
        block.translation = "Комикс транслятор"
        block.fontformat.font_size = 18

        item = TextBlkItem(block, 0)
        self.assertEqual(item.fontformat.shape_type, 'rect')

        # Toggle shape to ellipse
        item.setShapeType('ellipse')
        self.assertEqual(item.fontformat.shape_type, 'ellipse')
        self.assertEqual(item.blk.fontformat.shape_type, 'ellipse')

        # Toggle hyphenation
        item.setAutoHyphenate(True)
        self.assertTrue(item.fontformat.auto_hyphenate)
        self.assertIn('\u00ad', item.toPlainText())

        # Untoggle hyphenation
        item.setAutoHyphenate(False)
        self.assertFalse(item.fontformat.auto_hyphenate)
        self.assertNotIn('\u00ad', item.toPlainText())
        self.assertEqual(item.toPlainText(), "Комикс транслятор")


if __name__ == '__main__':
    unittest.main()
