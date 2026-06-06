#!/usr/bin/env python3
"""Parse paper.md and generate image → figure label mapping.

Reads the structured markdown output from minerU and extracts figure/table
labels from the text immediately following each ![](...) image reference.

Subfigures (consecutive images without caption text between them) are grouped
and assigned (a), (b), (c) sub-labels under the next caption found.

Usage:
    map_mineru_images.py -m <paper.md> -o <image-map.txt>
"""

import argparse
import re
import sys
from pathlib import Path


# Patterns for figure/table labels in caption text
FIG_PATTERN = re.compile(
    r"(?:FIG|Figure|Fig\.?)\s*\.?\s*(\d+)(?:[\.\s]*\()?([a-zA-Z])?",
    re.IGNORECASE,
)
TABLE_PATTERN = re.compile(
    r"(?:TABLE|Table)\s*\.?\s*(\d+)(?:[\.\s]*\()?([a-zA-Z])?",
    re.IGNORECASE,
)
IMAGE_REF = re.compile(r'!\[\]\(images/([^)]+\.jpg)\)')


def extract_label(text: str) -> str | None:
    """Find the first figure/table label in text."""
    m = TABLE_PATTERN.search(text)
    if m:
        tag = f"TABLE {m.group(1)}"
        sub = m.group(2)
        return f"{tag}({sub})" if sub else tag
    m = FIG_PATTERN.search(text)
    if m:
        tag = f"FIG. {m.group(1)}"
        sub = m.group(2)
        return f"{tag}({sub})" if sub else tag
    return None


def build_image_map(md_path: Path, output_path: Path):
    """Extract image→figure mapping from paper.md content structure."""
    content = md_path.read_text(encoding="utf-8", errors="replace")

    # Phase 1: collect all image references with positions
    refs = list(IMAGE_REF.finditer(content))
    if not refs:
        # Write empty header
        output_path.write_text(
            "# minerU Image → Figure Mapping (from paper.md)\n"
            "# No image references found in paper.md.\n"
        )
        return

    result = []
    pending = []
    prev_base = None
    sub_count = 0

    for m in refs:
        filename = m.group(1)
        after = content[m.end():m.end() + 400]
        label = extract_label(after)

        if label:
            base = re.sub(r'\s*\([a-z]\)$', '', label).strip()
            if base == prev_base:
                sub_count += 1
                label = f"{base}({chr(ord('a')+sub_count)})"
            else:
                sub_count = 0
                prev_base = base
            result.append((filename, label))
        else:
            pending.append((filename, None))

    for i, (fname, _) in enumerate(pending):
        result.append((fname, f"FIG. ??({chr(ord('a')+i)})"))

    # Phase 4: write output
    lines = [
        "# minerU Image → Figure Mapping (from paper.md)",
        "# Subfigures are grouped by consecutive ![](...) refs",
        "# Format: <filename>  →  <label>",
    ]
    for filename, label in result:
        lines.append(f"{filename}  →  {label}")
    output_path.write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Generate image→figure map from minerU paper.md"
    )
    parser.add_argument("-m", "--md", required=True, help="Path to paper.md")
    parser.add_argument("-o", "--output", required=True, help="Output image-map.txt")
    args = parser.parse_args()

    md_path = Path(args.md)
    if not md_path.exists():
        print(f"Error: {args.md} not found", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output)
    build_image_map(md_path, output_path)

    # Print summary
    lines = output_path.read_text().strip().splitlines()
    data_lines = [l for l in lines if l and not l.startswith("#")]
    print(f"  {len(data_lines)} images mapped from {md_path.name}")


if __name__ == "__main__":
    main()
