#!/usr/bin/env python3
"""Parse-run bookkeeping — single owner of name, skip key, and manifest schema.

The three parse paths (CLI wrapper, router batch, HTTP client) used to each
derive a paper's name, check a skip condition, and write a manifest with
their own vocabulary. This module is the one implementation of those
glossary concepts (derived name, skip key, manifest, status):

  * derived name — `derive_name(pdf_path)`: the stable key that decides the
    output dir and the skip key
  * skip key — `paper_md_path(parsed_dir, name)` / `is_skipped(parsed_dir,
    name)`: `parsed/<name>/paper.md` existing means the input is already
    parsed and defaults to being skipped
  * manifest — `manifest_payload(settings, rows)`: one schema with rows in
    the glossary status vocabulary {parsed, failed, skipped} and a
    `summary {total, parsed, failed, skipped}` block
"""

from pathlib import Path


def derive_name(pdf_path: str) -> str:
    """Derive a clean paper key from a PDF filename.

    Strips extension, replaces non-alphanumeric with underscores, collapses
    consecutive underscores, strips leading/trailing ones.
    """
    name = Path(pdf_path).stem
    name = "".join(c if c.isalnum() else "_" for c in name)
    while "__" in name:
        name = name.replace("__", "_")
    name = name.strip("_")
    return name if name else "unnamed"


def paper_md_path(parsed_dir: Path, name: str) -> Path:
    """The skip key for one paper: <parsed_dir>/<name>/paper.md."""
    return parsed_dir / name / "paper.md"


def is_skipped(parsed_dir: Path, name: str) -> bool:
    """True when the paper's artifact already exists (default skip)."""
    return paper_md_path(parsed_dir, name).exists()


def paper_status(paper_md: Path, *, skipped: bool = False) -> str:
    """Glossary status for a paper: 'skipped' (artifact existed, not re-parsed),
    else 'parsed' when the artifact exists, else 'failed'."""
    if skipped:
        return "skipped"
    return "parsed" if paper_md.exists() else "failed"


def paper_row(name: str, pdf_path: str, paper_md: str, status: str, **extra) -> dict:
    """One manifest row, in the glossary vocabulary. extra carries
    caller-specific fields (batch's time/images/error/retries)."""
    return {
        "name": name,
        "pdf_path": pdf_path,
        "paper_md": paper_md,
        "status": status,
        **extra,
    }


def manifest_payload(settings: dict, rows: list[dict]) -> dict:
    """One manifest schema for every parse run.

    settings: caller-specific run settings. rows: one dict per input, using
    the glossary status vocabulary {parsed, failed, skipped}; every row
    carries at least {"name", "status"}. The summary block is computed from
    the rows — never passed in, so the three writers cannot drift.
    """
    parsed = sum(1 for r in rows if r.get("status") == "parsed")
    failed = sum(1 for r in rows if r.get("status") == "failed")
    skipped = sum(1 for r in rows if r.get("status") == "skipped")
    return {
        "settings": settings,
        "summary": {
            "total": len(rows),
            "parsed": parsed,
            "failed": failed,
            "skipped": skipped,
        },
        "papers": rows,
    }