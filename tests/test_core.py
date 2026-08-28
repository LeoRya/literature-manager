import shutil
import tempfile
import unittest
from pathlib import Path

import fitz

from literature_manager.database import Database
from literature_manager.import_export import export_file, import_file
from literature_manager.scanner import LibraryScanner
from literature_manager.search import SearchCondition, SearchService


def make_pdf(path: Path, text: str) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.set_metadata({"title": "Space Charge Effects", "author": "Ada Beam; Li Ming"})
    document.save(path)
    document.close()


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.database = Database(self.folder / "library.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_search_supports_global_and_nested_logic(self):
        paper_id, _ = self.database.insert_imported(
            {
                "title": "Space charge instabilities",
                "authors": "Ada Beam; Li Ming",
                "journal": "Accelerator Review",
                "year": "2025",
                "doi": "10.1000/space",
                "keywords_auto": "space charge; damping",
            },
            ["核心", "第三章"],
        )
        self.database.insert_imported(
            {
                "title": "Superconducting cavities",
                "authors": "Grace Linac",
                "year": "2024",
                "doi": "10.1000/rf",
            },
            ["待读"],
        )
        service = SearchService(self.database)
        self.assertEqual([row["id"] for row in service.search("第三章")], [paper_id])
        conditions = [
            SearchCondition("authors", ["Ada", "Li Ming"], "AND"),
            SearchCondition("tags", ["核心"], "OR"),
        ]
        self.assertEqual([row["id"] for row in service.search("", conditions, "AND")], [paper_id])

    def test_scan_is_incremental_and_deduplicates_locations(self):
        root = self.folder / "papers"
        nested = root / "nested"
        nested.mkdir(parents=True)
        first = nested / "paper.pdf"
        make_pdf(
            first,
            "Space Charge Effects\nDOI: 10.1234/example.2025\nKeywords: beam; damping\nPublished 2025 " * 4,
        )
        scanner = LibraryScanner(self.database)
        self.assertEqual(scanner.scan(root, online=False)["added"], 1)
        self.assertEqual(scanner.scan(root, online=False)["skipped"], 1)
        duplicate = root / "copy.pdf"
        shutil.copy2(first, duplicate)
        scanner.scan(root, online=False)
        ids = self.database.all_paper_ids()
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(self.database.get_paper(ids[0])["files"]), 2)
        self.assertEqual(len(SearchService(self.database).search("damping")), 1)

    def test_broken_pdf_remains_visible_as_error(self):
        root = self.folder / "broken"
        root.mkdir()
        (root / "broken.pdf").write_bytes(b"not a PDF")
        result = LibraryScanner(self.database).scan(root, online=False)
        self.assertEqual(result["failed"], 1)
        paper = self.database.get_paper(self.database.all_paper_ids()[0])
        self.assertEqual(paper["status"], "error")
        self.assertTrue(paper["files"][0]["error_message"])

    def test_csv_bibtex_and_ris_roundtrip(self):
        self.database.insert_imported(
            {
                "title": "A Test Paper",
                "authors": "Ada Beam; Li Ming",
                "journal": "Test Journal",
                "year": "2025",
                "doi": "10.1234/test",
                "keywords_auto": "beam; test",
            },
            ["核心"],
        )
        for suffix in (".csv", ".bib", ".ris"):
            path = self.folder / f"export{suffix}"
            self.assertEqual(export_file(self.database, path), 1)
            target = Database(self.folder / f"target-{suffix[1:]}.db")
            self.assertEqual(import_file(target, path), (1, 0))
            imported = target.get_paper(target.all_paper_ids()[0])
            self.assertEqual(imported["doi"], "10.1234/test")


if __name__ == "__main__":
    unittest.main()
