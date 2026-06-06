#!/usr/bin/env python3
"""Parse minerU content_list_v2.json and generate image→figure mapping.

Groups images by figure using bbox layout:
- Full-width items (spanning >60% of page) are standalone
- If a page has exactly one captioned figure: all items on the page
  are subfigures of that figure, ordered by reading order
- If a page has multiple captioned figures: each is standalone

Usage:
    python3 map_mineru_images.py \
        -i <output_dir>/auto/content_list_v2.json \
        -o <output_dir>/auto/image-map.txt
"""

import argparse
import json
import os
import re
from pathlib import Path


ROW_TOLERANCE = 30
FULL_WIDTH_RATIO = 0.6
X_GAP_TOLERANCE = 50  # px — adjacent columns with gap < this are same figure


def parse_caption(caption_items):
    if not caption_items:
        return ""
    return "".join(
        item.get("content", "") for item in caption_items if isinstance(item, dict)
    )


def extract_figure_label(text):
    m = re.search(
        r'\b(FIG\.?\s*\d+[a-z]?|Figure\s*\d+[a-z]?|TABLE\s+[IVXLCDM]+|Table\s+\d+)',
        text, re.IGNORECASE,
    )
    return m.group(0) if m else None


def page_width(items):
    """Estimate page width from max bbox x2 coordinate."""
    max_x = 0
    for _, _, _, bbox in items:
        if bbox[2] > max_x:
            max_x = bbox[2]
    return max(max_x, 1)


def is_full_width(bbox, max_x):
    return (bbox[2] - bbox[0]) > FULL_WIDTH_RATIO * max_x


def x_adjacent(bbox1, bbox2):
    """True if bboxes share x-range or have a small gap."""
    if (bbox1[2] < bbox2[0] or bbox2[2] < bbox1[0]):
        # No overlap — check gap
        gap = min(abs(bbox1[2] - bbox2[0]), abs(bbox2[2] - bbox1[0]))
        return gap <= X_GAP_TOLERANCE
    return True


def group_by_adjacency(items):
    """Partition items into connected groups via x-adjacency."""
    if not items:
        return []
    remaining = list(range(len(items)))
    groups = []
    while remaining:
        stack = [remaining.pop(0)]
        group = []
        while stack:
            idx = stack.pop()
            group.append(items[idx])
            i = 0
            while i < len(remaining):
                if x_adjacent(items[idx][3], items[remaining[i]][3]):
                    stack.append(remaining.pop(i))
                else:
                    i += 1
        groups.append(group)
    return groups


def group_into_rows(items, tolerance=ROW_TOLERANCE):
    """Partition items into rows by bbox y-coordinate."""
    if not items:
        return []
    sorted_items = sorted(items, key=lambda x: (x[3][1], x[3][0]))
    rows = []
    _, _, _, first_bbox = sorted_items[0]
    current_row = [sorted_items[0]]
    current_y = first_bbox[1]
    for item in sorted_items[1:]:
        _, _, _, bbox = item
        y = bbox[1]
        if abs(y - current_y) <= tolerance:
            current_row.append(item)
        else:
            current_row.sort(key=lambda x: x[3][0])
            rows.append(current_row)
            current_row = [item]
            current_y = y
    current_row.sort(key=lambda x: x[3][0])
    rows.append(current_row)
    return rows


def build_image_map(content_list_path):
    with open(content_list_path) as f:
        pages = json.load(f)

    # Phase 1: collect all visual items per page with bbox
    page_items = {}  # page_idx -> [(img_path, label, caption_text, bbox)]
    for page_idx, blocks in enumerate(pages):
        items = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            item_type = block.get("type")
            content = block.get("content", {})
            if not isinstance(content, dict):
                continue
            img_src = content.get("image_source", {}).get("path", "")

            if item_type in ("image", "chart", "table") and img_src:
                if item_type == "table":
                    cap_items = content.get("table_caption", [])
                elif item_type == "chart":
                    cap_items = content.get("chart_caption", [])
                else:
                    cap_items = content.get("image_caption", [])
                cap_text = parse_caption(cap_items)
                label = extract_figure_label(cap_text)
                bbox = block.get("bbox", [0, 0, 0, 0])
                items.append((img_src.strip(), label, cap_text, bbox))

        if items:
            page_items[page_idx] = items

    # Phase 2: assign labels
    image_groups = {}  # (page_idx, fig_label) -> ([img_paths], caption_text)

    for page_idx, items in page_items.items():
        max_x = page_width(items)

        # Separate full-width items (tables spanning the page)
        full_width_items = [it for it in items if is_full_width(it[3], max_x)]
        remaining = [it for it in items if not is_full_width(it[3], max_x)]

        # Count captioned items among remaining
        captioned = [it for it in remaining if it[1]]
        uncaptioned = [it for it in remaining if not it[1]]

        # Strategy: if exactly one captioned figure in remaining items,
        #   and there are uncaptioned items → all are subfigures
        # Otherwise: each captioned item is its own figure
        # Uncptioned items with no caption: group among themselves

        processed = set()

        if len(captioned) == 1 and uncaptioned:
            # One figure with multiple subfigures
            fig_label = captioned[0][1]
            main_cap = captioned[0][2]
            combined = remaining  # captioned + uncaptioned together
            adj_groups = group_by_adjacency(combined)
            for group in adj_groups:
                # Only include groups that contain the captioned item or
                # are adjacent to it
                rows = group_into_rows(group)
                sub_idx = 0
                for row in rows:
                    for img_path, _, _, bbox in row:
                        sub_label = chr(ord('a') + sub_idx)
                        sub_idx += 1
                        key = (page_idx, fig_label)
                        if key not in image_groups:
                            image_groups[key] = ([], main_cap)
                        image_groups[key][0].append((img_path, bbox, sub_label))
            for it in combined:
                processed.add(it[0])

        elif len(captioned) >= 2:
            # Multiple figures on the page — each captioned item is standalone
            for img_path, lab, cap, bbox in captioned:
                key = (page_idx, lab)
                if key not in image_groups:
                    image_groups[key] = ([], cap)
                image_groups[key][0].append((img_path, bbox, ""))
                processed.add(img_path)

        elif captioned and not uncaptioned:
            # All items are caption-bearing — each standalone
            for img_path, lab, cap, bbox in captioned:
                key = (page_idx, lab)
                if key not in image_groups:
                    image_groups[key] = ([], cap)
                image_groups[key][0].append((img_path, bbox, ""))
                processed.add(img_path)

        elif not captioned:
            # No captions — group remaining items
            adj_groups = group_by_adjacency(remaining)
            for group in adj_groups:
                if len(group) == 1:
                    key = (page_idx, "?")
                    if key not in image_groups:
                        image_groups[key] = ([], "")
                    image_groups[key][0].append((group[0][0], group[0][3], ""))
                else:
                    rows = group_into_rows(group)
                    sub_idx = 0
                    for row in rows:
                        for img_path, _, _, bbox in row:
                            sub_label = chr(ord('a') + sub_idx)
                            sub_idx += 1
                            key = (page_idx, "?")
                            if key not in image_groups:
                                image_groups[key] = ([], "")
                            image_groups[key][0].append((img_path, bbox, sub_label))

        # Handle full-width items — always standalone
        for img_path, lab, cap, bbox in full_width_items:
            label = lab if lab else "?"
            key = (page_idx, label)
            if key not in image_groups:
                image_groups[key] = ([], cap)
            image_groups[key][0].append((img_path, bbox, ""))

    # Phase 3: build output
    lines = [
        "# minerU Image → Figure Mapping (bbox-based)",
        "# Full-width items (tables) are standalone",
        "# Composite figures get (a), (b), ... by reading order",
        "# Format: <filename>  →  <label>  (page <N>)",
        "",
    ]

    for (page_idx, fig_label), (sub_items, _) in sorted(
        image_groups.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))
    ):
        for img_path, bbox, sub_letter in sub_items:
            fname = os.path.basename(img_path)
            label = f"{fig_label}({sub_letter})" if sub_letter else fig_label
            lines.append(f"{fname}  →  {label}  (page {page_idx})")

    output = "\n".join(lines) + "\n"
    return output, image_groups


def main():
    parser = argparse.ArgumentParser(
        description="Map minerU images to figure labels (bbox-aware)"
    )
    parser.add_argument("-i", "--input", required=True, help="Path to content_list_v2.json")
    parser.add_argument("-o", "--output", required=True, help="Output path for image-map.txt")
    args = parser.parse_args()

    output_text, image_groups = build_image_map(args.input)

    output_dir = Path(args.output).parent
    if not output_dir.exists():
        output_dir.mkdir(parents=True, exist_ok=True)

    with open(args.output, "w") as f:
        f.write(output_text)

    total = sum(len(subs) for subs, _ in image_groups.values())
    composite = sum(1 for subs, _ in image_groups.values() if len(subs) > 1)
    print(f"Wrote {total} mappings ({composite} composite) to {args.output}")
    for line in output_text.splitlines()[4:]:
        if line.strip():
            print(f"  {line}")


if __name__ == "__main__":
    main()