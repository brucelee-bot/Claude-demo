import unittest
from pathlib import Path


class GaoxinRdBatchWriteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "application_gaoxin_book.html"
        ).read_text(encoding="utf-8")

    def test_rd_section_has_batch_write_progress_controls(self):
        self.assertIn('id="rd-batch-write-button"', self.template)
        self.assertIn("一键撰写全部研发活动", self.template)
        self.assertIn('role="progressbar"', self.template)
        self.assertIn('id="rd-batch-progress-fill"', self.template)
        self.assertIn('aria-live="polite"', self.template)

    def test_batch_writer_generates_all_three_rd_fields_and_saves(self):
        self.assertIn("var rdBatchWriteFields = [", self.template)
        self.assertIn("{key: 'purpose'", self.template)
        self.assertIn("{key: 'innovation'", self.template)
        self.assertIn("{key: 'result'", self.template)
        self.assertIn("async function generateAllRdDescriptions(button)", self.template)
        self.assertIn("await generateRdDescriptionField", self.template)
        self.assertIn("await saveRdBatchContent()", self.template)
        self.assertIn("updateRdBatchProgress(position, tasks.length)", self.template)


if __name__ == "__main__":
    unittest.main()
