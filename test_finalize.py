#!/usr/bin/env python3
"""Unit tests for finalize.py — raw/final arrival, idempotency, orphan filter.

Run:  python3 test_finalize.py        (or: python3 -m unittest test_finalize -v)
No external deps. Covers the edge cases the old CLAUDE.md dry-run heredoc
exercised, plus final-mode arrivals (batch/HTTP callers) and the pure
orphan_jpgs decision.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from finalize import finalize_output, orphan_jpgs  # noqa: E402

JUNK = ("_layout.pdf", "_origin.pdf", "_span.pdf",
        "_middle.json", "_model.json", "_content_list_v2.json")


def make_raw(parent: Path, name: str, md: str, images: dict):
    """Mock the minerU auto/ layout: <parent>/<name>/auto/{<name>.md, images/, junk}."""
    auto = parent / name / "auto"
    auto.mkdir(parents=True)
    (auto / f"{name}.md").write_text(md)
    (auto / "images").mkdir()
    for fname, data in images.items():
        (auto / "images" / fname).write_bytes(data)
    for suffix in JUNK:
        (auto / f"{name}{suffix}").write_bytes(b"junk")
    return auto


def make_final(parent: Path, name: str, md: str, images: dict, with_map: bool):
    """Mock a batch/HTTP-arrival tree: <parent>/<name>/{paper.md, images/}."""
    paper = parent / name
    paper.mkdir(parents=True)
    (paper / "paper.md").write_text(md)
    (paper / "images").mkdir()
    for fname, data in images.items():
        (paper / "images" / fname).write_bytes(data)
    if with_map:
        (paper / "image-map.txt").write_text("c.jpg  \u2192  FIG. 9\n")
    return paper


class RawArrivalTest(unittest.TestCase):
    """minerU output trees (wrapper path)."""

    def test_single_mode_relocates_and_cleans(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            # xyz.jpg referenced by paper.md → kept; orphan.jpg in neither → dropped.
            make_raw(td / "single", "demo",
                     "# Mock\n\n![](images/xyz.jpg)\n",
                     {"xyz.jpg": b"jpg", "orphan.jpg": b"jpg"})
            r = finalize_output("demo", td / "single", td / "single" / "parsed")
            paper = td / "single" / "parsed" / "demo"
            imgs = paper / "images"
            self.assertTrue(r.exists() and r.name == "paper.md")
            self.assertTrue((imgs / "xyz.jpg").exists(), "md-referenced jpg survives")
            self.assertFalse((imgs / "orphan.jpg").exists(), "orphan jpg filtered")
            # image-map generated in-process, names the kept jpg
            self.assertTrue((paper / "image-map.txt").exists())
            self.assertIn("xyz.jpg", (paper / "image-map.txt").read_text())
            self.assertFalse((paper / "auto").exists(), "auto/ dropped")
            self.assertFalse((td / "single" / "demo").exists(), "raw wrapper removed")

    def test_batch_layout_keeps_wrapper(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            make_raw(td / "batch", "demo",
                     "# Mock\n\n![](images/xyz.jpg)\n",
                     {"xyz.jpg": b"jpg", "orphan.jpg": b"jpg"})
            r = finalize_output("demo", td / "batch", td / "batch")
            paper = td / "batch" / "demo"
            imgs = paper / "images"
            self.assertTrue(r.exists() and r.name == "paper.md")
            self.assertTrue((imgs / "xyz.jpg").exists())
            self.assertFalse((imgs / "orphan.jpg").exists())
            self.assertFalse((paper / "auto").exists())
            self.assertTrue(paper.exists(), "wrapper survives (batch layout)")

    def test_idempotent_rerun_over_fresh_output(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            make_raw(td / "batch", "demo",
                     "# Mock\n\n![](images/xyz.jpg)\n",
                     {"xyz.jpg": b"jpg", "orphan.jpg": b"jpg"})
            finalize_output("demo", td / "batch", td / "batch")
            # A fresh minerU output landing on top re-finalizes cleanly.
            make_raw(td / "batch", "demo",
                     "# Mock v2\n\n![](images/xyz.jpg)\n",
                     {"xyz.jpg": b"jpg", "orphan.jpg": b"jpg"})
            r = finalize_output("demo", td / "batch", td / "batch")
            self.assertTrue((td / "batch" / "demo" / "paper.md").exists())
            self.assertEqual(r, td / "batch" / "demo" / "paper.md")

    def test_missing_auto_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "missing" / "ghost").mkdir(parents=True)
            self.assertIsNone(
                finalize_output("ghost", td / "missing", td / "missing" / "parsed"))

    def test_raw_without_markdown_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            auto = td / "raw" / "demo" / "auto"
            auto.mkdir(parents=True)
            (auto / "images").mkdir()
            (auto / "junk.json").write_text("{}")
            self.assertIsNone(finalize_output("demo", td / "raw", td / "parsed"))


class FinalArrivalTest(unittest.TestCase):
    """Trees already at the final location (batch_parse / api_client paths)."""

    def test_generates_missing_image_map_and_filters(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            # a.jpg referenced by md, b.jpg an orphan; no image-map yet.
            make_final(td, "demo",
                       "# Mock\n\n![](images/a.jpg)\n\nFig. 1 caption\n",
                       {"a.jpg": b"jpg", "b.jpg": b"jpg"}, with_map=False)
            r = finalize_output("demo", td, td)
            paper = td / "demo"
            self.assertEqual(r, paper / "paper.md")
            self.assertTrue((paper / "image-map.txt").exists())
            self.assertIn("a.jpg", (paper / "image-map.txt").read_text())
            self.assertTrue((paper / "images" / "a.jpg").exists())
            self.assertFalse((paper / "images" / "b.jpg").exists())

    def test_existing_map_is_refreshed(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            # stale map lists c.jpg; paper.md references a.jpg; b.jpg orphan.
            make_final(td, "demo",
                       "# Mock\n\n![](images/a.jpg)\n\nFig. 1 caption\n",
                       {"a.jpg": b"jpg", "b.jpg": b"jpg", "c.jpg": b"jpg"},
                       with_map=True)
            finalize_output("demo", td, td)
            imgs = td / "demo" / "images"
            # map regenerated from current paper.md — stale c.jpg entry gone
            self.assertIn("a.jpg", (td / "demo" / "image-map.txt").read_text())
            self.assertNotIn("c.jpg", (td / "demo" / "image-map.txt").read_text())
            self.assertTrue((imgs / "a.jpg").exists(), "md-referenced jpg survives")
            self.assertFalse((imgs / "c.jpg").exists(), "stale map-only jpg dropped")
            self.assertFalse((imgs / "b.jpg").exists(), "orphan dropped")

    def test_missing_paper_md_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "ghost").mkdir()
            self.assertIsNone(finalize_output("ghost", td, td))


class OrphanJpgsTest(unittest.TestCase):
    """The pure decision function: reads, never mutates."""

    def test_kept_and_dropped_sets(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            imgs = td / "images"
            imgs.mkdir()
            (td / "image-map.txt").write_text("mapped.jpg  \u2192  FIG. 1\n")
            md = td / "paper.md"
            md.write_text("# x\n\n![](images/refd.jpg)\n")
            for f in ("mapped.jpg", "refd.jpg", "orphan.jpg"):
                (imgs / f).write_bytes(b"jpg")

            victims = orphan_jpgs(imgs, td / "image-map.txt", md)
            self.assertEqual([v.name for v in victims], ["orphan.jpg"])
            # no mutation
            self.assertTrue((imgs / "orphan.jpg").exists())

    def test_missing_map_falls_back_to_md_refs(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            imgs = td / "images"
            imgs.mkdir()
            (imgs / "refd.jpg").write_bytes(b"jpg")
            (imgs / "other.jpg").write_bytes(b"jpg")
            md = td / "paper.md"
            md.write_text("# x\n\n![](images/refd.jpg)\n")
            victims = orphan_jpgs(imgs, td / "no-map.txt", md)
            self.assertEqual([v.name for v in victims], ["other.jpg"])


if __name__ == "__main__":
    unittest.main(verbosity=2)