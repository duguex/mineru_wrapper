#!/usr/bin/env python3
"""Parse paper.md and generate image → figure label mapping.

Reads the structured markdown output from minerU and extracts figure/table
labels from the text immediately following each ![](...) image reference.

Subfigures (consecutive images sharing the same base label) are grouped
and assigned (a), (b), (c) sub-labels in document order. Images without
an inline caption inherit the preceding figure's base label, so they
land in the right group instead of being dropped into a separate bucket.

Usage:
    map_mineru_images.py -m <paper.md> -o <image-map.txt>
"""

import argparse
import re
import sys
from itertools import groupby
from pathlib import Path


# Caption patterns. The number group accepts:
#   - plain arabic:        Fig. 1, Figure 6
#   - SI / supplementary:  Fig. S1, Fig. S11
#   - chapter style:       Fig. 1.1, Fig. 2.3 (common in book chapters)
# TABLE additionally accepts roman: TABLE IV (Phys Rev classics).
FIG_PATTERN = re.compile(
    r"(?:FIG|Figure|Fig\.?)\s*\.?\s*(S?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
TABLE_PATTERN = re.compile(
    r"(?:TABLE|Table)\s*\.?\s*(S?\d+(?:\.\d+)?|[IVXLCDM]+)",
    re.IGNORECASE,
)
IMAGE_REF = re.compile(r'!\[\]\(images/([^)]+\.jpg)\)')

# Search this far past an image reference for its caption text.
CAPTION_WINDOW = 400


def extract_base(text: str) -> str | None:
    """Find the figure/table BASE label that comes first in text.

    Returns 'TABLE N' / 'FIG. N' or None. The earliest match wins so a
    real Figure caption beats a mid-paragraph 'see Table I' reference.
    Sub-letters are discarded here — the grouping pass assigns them from
    document order instead.
    """
    candidates = []
    m = FIG_PATTERN.search(text)
    if m:
        candidates.append((m.start(), f"FIG. {m.group(1)}"))
    m = TABLE_PATTERN.search(text)
    if m:
        candidates.append((m.start(), f"TABLE {m.group(1)}"))
    if not candidates:
        return None
    return min(candidates, key=lambda c: c[0])[1]


def sub_label(i: int) -> str:
    """Generate a sub-figure letter from a 0-based index.

    0..25  → 'a'..'z'
    26..51 → 'aa'..'az'
    52..   → 'ba'..'zz'  (676 slots total)

    The naive chr(ord('a') + i) overflows at i=26 ('{') and worse — at
    i=36 it produces \\x85 (U+0085 NEXT LINE), which Python's splitlines()
    treats as a line break and corrupts image-map.txt.
    """
    if i < 26:
        return chr(ord('a') + i)
    if i < 26 * 27:  # 26 + 26*26
        first = chr(ord('a') + (i - 26) // 26)
        second = chr(ord('a') + (i - 26) % 26)
        return first + second
    # >676 sub-figures in one group is almost certainly a mapper error;
    # fall back to a clearly-marked numeric tail so the entry is still
    # parseable and visible in image-map.txt.
    return f"sub{i}"


def build_image_map(md_path: Path, output_path: Path):
    """Extract image → figure mapping from paper.md content structure."""
    content = md_path.read_text(encoding="utf-8", errors="replace")

    refs = list(IMAGE_REF.finditer(content))
    if not refs:
        output_path.write_text(
            "# minerU Image → Figure Mapping (from paper.md)\n"
            "# No image references found in paper.md.\n"
        )
        return

    # Phase 1 — assign every ref a base label. Images without an inline
    # caption inherit the previous base (typical when multiple sub-images
    # sit between one caption block and the next paragraph).
    items = []  # [(filename, base)]
    prev_base = "FIG. ??"  # fallback for refs that precede any caption
    for m in refs:
        filename = m.group(1)
        after = content[m.end():m.end() + CAPTION_WINDOW]
        base = extract_base(after)
        if base:
            prev_base = base
        items.append((filename, prev_base))

    # Phase 2 — group consecutive items by base. ≥2 in a group → (a),(b),(c)…;
    # singletons keep the bare base label.
    result = []
    for base, grp in groupby(items, key=lambda x: x[1]):
        grp = list(grp)
        if len(grp) == 1:
            result.append((grp[0][0], base))
        else:
            for i, (fname, _) in enumerate(grp):
                result.append((fname, f"{base}({sub_label(i)})"))

    # Phase 3 — write output.
    lines = [
        "# minerU Image → Figure Mapping (from paper.md)",
        "# Consecutive images sharing a label become (a),(b),(c)…",
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

    lines = output_path.read_text().strip().splitlines()
    data_lines = [l for l in lines if l and not l.startswith("#")]
    print(f"  {len(data_lines)} images mapped from {md_path.name}")


if __name__ == "__main__":
    main()
