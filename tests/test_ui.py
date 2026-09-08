import re
import unittest
from pathlib import Path


HTML = Path(__file__).parents[1] / "web" / "static" / "index.html"


class WorkspaceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = HTML.read_text(encoding="utf-8")

    def test_editorial_workspace_keeps_dataset_panel_and_pipeline_controls(self):
        for marker in (
            "workspace-shell",
            "workspace-sidebar",
            'id="nemotronDataset"',
            'id="datasetMeta"',
            'id="datasetLockNote"',
            "setPipelineBusy",
            "dataset: runDataset",
            "source_label",
            'for="entityText"',
            'for="goalText"',
            'for="cohortDesc"',
            "if (d.error)",
            "table-scroll",
        ):
            self.assertIn(marker, self.source)

    def test_dataset_selector_is_keyboard_accessible(self):
        self.assertIn('aria-label="Panel configuration"', self.source)
        self.assertIn('aria-live="polite"', self.source)
        self.assertGreaterEqual(self.source.count('class="template-chip"'), 6)
        self.assertEqual(self.source.count('class="template-chip"'), self.source.count('type="button" class="template-chip"'))

    def test_pipeline_captures_and_restores_selection(self):
        start = self.source.index("async function runFullPipeline")
        end = self.source.index("function rerunWithCalibration", start)
        body = self.source[start:end]
        self.assertIn("const runDataset = selectedDataset", body)
        self.assertIn("setPipelineBusy(true)", body)
        self.assertIn("setPipelineBusy(false)", body)
        self.assertIn("if (timerInterval) clearInterval(timerInterval)", body)

    def test_no_duplicate_dom_ids(self):
        ids = re.findall(r'\bid="([^"]+)"', self.source)
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        self.assertEqual(duplicates, [], f"duplicate ids: {duplicates}")


if __name__ == "__main__":
    unittest.main()
