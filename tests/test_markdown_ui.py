import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
HTML = ROOT / "web" / "static" / "index.html"
VENDOR = ROOT / "web" / "static" / "vendor"


class MarkdownUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = HTML.read_text(encoding="utf-8")

    def test_pinned_local_markdown_dependencies_are_loaded(self):
        self.assertTrue((VENDOR / "marked.min.js").is_file())
        self.assertTrue((VENDOR / "purify.min.js").is_file())
        self.assertTrue((VENDOR / "LICENSE.marked.txt").is_file())
        self.assertTrue((VENDOR / "LICENSE.dompurify.txt").is_file())
        self.assertIn("vendor/marked.min.js", self.source)
        self.assertIn("vendor/purify.min.js", self.source)
        vendor_notes = (VENDOR / "README.md").read_text(encoding="utf-8")
        self.assertIn("marked `18.0.11`", vendor_notes)
        self.assertIn("DOMPurify `3.4.14`", vendor_notes)

    def test_draft_and_analysis_have_edit_preview_controls(self):
        for marker in (
            'id="entityEditToggle"', 'id="entityPreviewToggle"',
            'id="entityPreview"', 'id="analysisRenderToggle"',
            'id="analysisSourceToggle"', 'id="analysisRendered"',
            'function renderMarkdown', 'DOMPurify.sanitize',
        ):
            self.assertIn(marker, self.source)
        self.assertIn('aria-pressed="true"', self.source)

    def test_raw_entity_value_is_still_used_for_pipeline(self):
        start = self.source.index("async function runFullPipeline")
        end = self.source.index("function rerunWithCalibration", start)
        body = self.source[start:end]
        self.assertIn("document.getElementById('entityText').value", body)
        self.assertIn("entity_text: text", body)


if __name__ == "__main__":
    unittest.main()
