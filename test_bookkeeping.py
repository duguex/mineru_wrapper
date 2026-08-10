#!/usr/bin/env python3
"""Unit tests for bookkeeping.py — derived name, skip key, manifest schema.

Run:  python3 test_bookkeeping.py     (or: python3 -m unittest test_bookkeeping -v)
No external deps.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bookkeeping import (  # noqa: E402
    derive_name,
    is_skipped,
    manifest_payload,
    paper_md_path,
    paper_row,
    paper_status,
)


class DeriveNameTest(unittest.TestCase):
    def test_plain_stem(self):
        self.assertEqual(derive_name("paper.pdf"), "paper")

    def test_upper_extension(self):
        self.assertEqual(derive_name("Paper.PDF"), "Paper")

    def test_special_chars_collapse(self):
        # spaces / hyphens / underscores → one underscore, CKJ kept
        self.assertEqual(derive_name("Foo - 2020 - bar.pdf"), "Foo_2020_bar")
        self.assertEqual(derive_name("论文_等 - 2000 - x.pdf"), "论文_等_2000_x")

    def test_leading_trailing_underscores_stripped(self):
        self.assertEqual(derive_name("__a__b__.pdf"), "a_b")

    def test_all_special_falls_back_to_unnamed(self):
        self.assertEqual(derive_name("..."), "unnamed")


class SkipKeyTest(unittest.TestCase):
    def test_paper_md_path_shape(self):
        with tempfile.TemporaryDirectory() as td:
            p = paper_md_path(Path(td), "demo")
            self.assertEqual(p, Path(td) / "demo" / "paper.md")

    def test_is_skipped_follows_artifact_exists(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            self.assertFalse(is_skipped(td, "demo"))
            (td / "demo").mkdir()
            (td / "demo" / "paper.md").write_text("# x")
            self.assertTrue(is_skipped(td, "demo"))


class PaperRowTest(unittest.TestCase):
    def test_paper_status_by_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            missing = paper_md_path(td, "ghost")
            self.assertEqual(paper_status(missing), "failed")
            (td / "demo").mkdir()
            (td / "demo" / "paper.md").write_text("# x")
            self.assertEqual(paper_status(paper_md_path(td, "demo")), "parsed")
            self.assertEqual(paper_status(missing, skipped=True), "skipped")
            self.assertEqual(paper_status(paper_md_path(td, "demo"), skipped=True), "skipped")

    def test_paper_row_shape_and_extra(self):
        row = paper_row("a", "/p/a.pdf", "/out/parsed/a/paper.md", "parsed",
                        time=1.2, images=3)
        self.assertEqual(row, {
            "name": "a",
            "pdf_path": "/p/a.pdf",
            "paper_md": "/out/parsed/a/paper.md",
            "status": "parsed",
            "time": 1.2,
            "images": 3,
        })


class ManifestPayloadTest(unittest.TestCase):
    def test_summary_computed_from_rows(self):
        rows = [
            {"name": "a", "status": "parsed"},
            {"name": "b", "status": "parsed"},
            {"name": "c", "status": "failed"},
            {"name": "d", "status": "skipped"},
        ]
        m = manifest_payload({"source": ["x.pdf"]}, rows)
        self.assertEqual(
            m["summary"], {"total": 4, "parsed": 2, "failed": 1, "skipped": 1})
        self.assertEqual(m["settings"], {"source": ["x.pdf"]})
        self.assertIs(m["papers"], rows, "rows passed through, not copied")

    def test_empty_rows_zero_summary(self):
        m = manifest_payload({}, [])
        self.assertEqual(
            m["summary"], {"total": 0, "parsed": 0, "failed": 0, "skipped": 0})


if __name__ == "__main__":
    unittest.main(verbosity=2)