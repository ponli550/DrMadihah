"""`umcares ingest` — pre-production material into an author brief."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from umcares import ingest

CSV = "Timestamp,Name,Age,Feedback\r\n" \
      "8/15/2026 14:00:00,Osman,18,\"scam siber, prihatin\"\r\n" \
      "8/15/2026 15:30:00,Nur,17,Kesedaran meningkat\r\n"


class Fetch(unittest.TestCase):
    def test_reads_local_csv_with_bom(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "responses.csv"
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write("\ufeff" + CSV)
            self.assertEqual(ingest.fetch_csv(str(p)), CSV)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            ingest.fetch_csv("/nonexistent/responses.csv")


class Parse(unittest.TestCase):
    def test_parses_forms_export(self):
        parsed = ingest.parse_responses(CSV)
        self.assertEqual(len(parsed["columns"]), 4)
        self.assertEqual(len(parsed["responses"]), 2)
        self.assertEqual(parsed["responses"][0]["Name"], "Osman")

    def test_empty_rows_are_dropped(self):
        parsed = ingest.parse_responses("A,B\r\n\r\n1,2\r\n")
        self.assertEqual(len(parsed["responses"]), 1)

    def test_empty_input(self):
        self.assertEqual(ingest.parse_responses(""), {"columns": [], "responses": []})


class Brief(unittest.TestCase):
    def test_builds_markdown_table(self):
        md = ingest.build_brief(ingest.parse_responses(CSV),
                                csv_source="https://x/csv", notebook="https://nb")
        self.assertIn("| Osman", md)
        self.assertIn("notebook: <https://nb>", md)
        self.assertIn("responses: 2", md)

    def test_max_rows_truncates(self):
        many = "A\n" + "".join(f"r{i}\n" for i in range(10))
        md = ingest.build_brief(ingest.parse_responses(many), max_rows=3)
        self.assertIn("7 more responses", md)

    def test_writes_md_and_json(self):
        with TemporaryDirectory() as d:
            out = ingest.write(ingest.parse_responses(CSV), Path(d) / "brief.md")
            self.assertTrue(out.exists())
            self.assertTrue(out.with_name("brief.json").exists())


if __name__ == "__main__":
    unittest.main()