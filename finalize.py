#!/usr/bin/env python3
"""Finalize a parse run's output into the canonical artifact subtree.

Single module owned by all three parse paths (CLI wrapper, router batch,
HTTP client). Deepens the old per-path post-processing (subprocess mapper +
another orphan filter per caller) into one in-process step:

  * raw arrival — minerU's `<src>/<name>/auto/` tree: rename the markdown to
    paper.md, move images/, regenerate image-map.txt in-process, drop the
    auxiliary junk minerU leaves behind
  * final arrival — a tree already written as `parsed/<name>/{paper.md,
    images/}` by batch/HTTP callers: regenerate image-map.txt from the
    current paper.md, drop orphan images

Idempotent. The image-map is always regenerated (never "if missing") — a
stale map from an earlier run must not stay authoritative. Image-map
generation failure is non-fatal: the input still finalizes to paper.md
without an image-map (the orphan filter then keeps anything paper.md
references and drops only unreferenced files).

Usage from a parse path:

    from finalize import finalize_output
    paper_md = finalize_output(name, src_tree, target_root)
"""

import re
import shutil
from pathlib import Path

from map_mineru_images import build_image_map

# Image reference inside paper.md markdown, e.g. ![](images/a1b2c3d4.jpg).
_IMAGE_REF_RE = re.compile(r"\(images/(\S+\.jpg)")


def orphan_jpgs(
    images_dir: Path, map_path: Path | None, md_path: Path | None
) -> list[Path]:
    """Return the jpgs in images_dir referenced by neither image-map nor paper.md.

    Pure decision — no filesystem mutation. The image-map names are the source
    of truth; paper.md image references are the fallback. A jpg in neither is
    a duplicate of content paper.md already expresses structurally (LaTeX
    formulas, markdown tables) and should be dropped.
    """
    mapped = set()
    if map_path is not None and map_path.exists():
        for line in map_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                fname = line.split("→", 1)[0].strip()
                if fname:
                    mapped.add(fname)
    md_refs = set()
    if md_path is not None and md_path.exists():
        md_refs = set(_IMAGE_REF_RE.findall(md_path.read_text()))
    return [f for f in images_dir.glob("*.jpg")
            if f.name not in mapped and f.name not in md_refs]


def _drop_orphans(paper_dir: Path) -> None:
    images_dir = paper_dir / "images"
    if images_dir.is_dir():
        for f in orphan_jpgs(
            images_dir, paper_dir / "image-map.txt", paper_dir / "paper.md"
        ):
            f.unlink()


def finalize_output(name: str, src_tree: Path, target_root: Path) -> Path | None:
    """Finalize one paper; return its final paper.md path, or None.

    Arrival shape is detected: `<src_tree>/<name>/auto/` present → raw mode
    (minerU output needing relocation); otherwise the tree is treated as
    already final (batch/HTTP callers writing `parsed/<name>/` directly).

    Raw mode: paper_dir = target_root/<name>; the markdown is renamed to
    paper.md, images/ is moved, image-map.txt is generated in-process, and
    everything left in auto/ is dropped. The raw wrapper dir is removed when
    it differs from paper_dir and is now empty (the CLI's single-shared
    layout). When src_tree == target_root, paper_dir == the raw wrapper and
    no removal happens.

    Final mode: paper_dir = src_tree/<name> (== target_root/<name>); no move,
    only image-map regeneration (from the current paper.md) and orphan
    filtering.

    Returns None when there is nothing to finalize (minerU produced no output,
    or no paper.md exists at the arrival site).
    """
    raw_dir = src_tree / name
    auto_dir = raw_dir / "auto"

    if auto_dir.is_dir():
        # ---- raw arrival: minerU wrote <name>/auto/ into src_tree ----
        paper_dir = target_root / name
        paper_dir.mkdir(parents=True, exist_ok=True)

        # 1. Rename the markdown to paper.md. minerU produces a single
        #    <name>.md; glob+first-hit guards against future variants.
        md_src = next(iter(auto_dir.glob("*.md")), None)
        if md_src is None:
            # minerU produced no markdown — nothing finalizable.
            return None
        md_dst = paper_dir / "paper.md"
        md_dst.unlink(missing_ok=True)
        shutil.move(str(md_src), str(md_dst))

        # 2. Move images/ (replace any existing copy).
        src_images = auto_dir / "images"
        if src_images.is_dir():
            dst_images = paper_dir / "images"
            if dst_images.exists():
                shutil.rmtree(str(dst_images))
            shutil.move(str(src_images), str(dst_images))

        # 3. Generate image-map.txt in-process from the final paper.md.
        #    Non-fatal: on failure the tree still finalizes without a map and
        #    the orphan filter falls back to paper.md references only.
        try:
            build_image_map(md_dst, paper_dir / "image-map.txt")
        except Exception:
            pass

        # 4. Drop orphan images.
        _drop_orphans(paper_dir)

        # 5. Everything still in auto/ is minerU auxiliary output — drop it.
        shutil.rmtree(str(auto_dir), ignore_errors=True)

        # 6. Remove the now-empty raw wrapper, but only when it differs from
        #    paper_dir (single-mode layout; batch layout keeps it).
        if raw_dir != paper_dir and raw_dir.is_dir() and not any(raw_dir.iterdir()):
            raw_dir.rmdir()

        return paper_dir / "paper.md"

    # ---- final arrival: tree already at target_root/<name> ----
    paper_dir = raw_dir
    md = paper_dir / "paper.md"
    if not md.exists():
        return None
    # Regenerate the map from the current paper.md — not "if missing". A
    # re-finalized tree must not keep a stale map authoritative: a map that
    # no longer matches paper.md preserves dead labels and dead images.
    try:
        build_image_map(md, paper_dir / "image-map.txt")
    except Exception:
        pass
    _drop_orphans(paper_dir)
    return md