#!/usr/bin/env python3
"""minerU wrapper — single PDF or batch parsing with env setup.

Wraps minerU: sources ROCm env, sets GPU, runs parsing, verifies output,
and generates image maps. One command, no raw CLI details in the skill.

Usage:
    # Single PDF (for `create` action)
    mineru_wrapper.py --single paper.pdf [output_dir]

    # Batch directory (for `batch-create` action)
    mineru_wrapper.py --batch pdf_dir/ [--force]

Logs: ~/logs/mineru/run_<timestamp>.log (full stdout + stderr per run)
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def derive_name(pdf_path: str) -> str:
    """Derive a clean paper key from a PDF filename.

    Strips extension, replaces non-alphanumeric with underscores,
    collapses consecutive underscores, strips leading/trailing ones.
    """
    name = Path(pdf_path).stem
    name = "".join(c if c.isalnum() else "_" for c in name)
    while "__" in name:
        name = name.replace("__", "_")
    name = name.strip("_")
    return name if name else "unnamed"


def mineru_available() -> bool:
    """Check whether the minerU conda env exists."""
    result = subprocess.run(
        ["conda", "run", "-n", "torch_rocm72", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0


def run_mineru(input_path: Path, output_dir: Path) -> bool:
    """Run minerU with persistent logging. Output streams to both terminal and log file."""
    log_dir = Path.home() / "logs" / "mineru"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    env_script = os.path.expanduser("~/mineru-rocm/mineru-rocm-env.sh")
    cmd = (
        f"export MINERU_API_MAX_CONCURRENT_REQUESTS=1 && "
        f"source {shlex.quote(env_script)} && "
        f"export HIP_VISIBLE_DEVICES=1 && "
        f"export PATH=/opt/conda/bin:$PATH && "
        f"conda run -n torch_rocm72 mineru -p {shlex.quote(str(input_path))} "
        f"-o {shlex.quote(str(output_dir))} -b pipeline -m auto -l en"
    )

    print(f"  Log: {log_path}")
    with open(log_path, "w") as log:
        log.write(f"=== minerU wrapper run {datetime.now()} ===\n")
        log.write(f"cmd: {cmd}\n\n")
        proc = subprocess.Popen(
            ["bash", "-c", cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, text=True,
        )
        for line in iter(proc.stdout.readline, ""):
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
        proc.wait()

    if proc.returncode != 0:
        print(f"minerU failed (exit {proc.returncode}). Log: {log_path}", file=sys.stderr)
        return False
    return True


def generate_image_map(parsed_name: str, parsed_dir: Path) -> dict:
    map_script = Path(__file__).resolve().parent / "map_mineru_images.py"
    content_json = parsed_dir / "auto" / f"{parsed_name}_content_list_v2.json"
    image_map = parsed_dir / "auto" / "image-map.txt"

    if not content_json.exists():
        return {"success": False, "error": f"content_list_v2.json not found at {content_json}"}

    result = subprocess.run(
        [sys.executable, str(map_script), "-i", str(content_json),
         "-o", str(image_map)],
        capture_output=True, text=True, timeout=60,
    )
    return {
        "success": result.returncode == 0,
        "error": result.stderr.strip() if result.returncode != 0 else None,
    }


def standardize_output(name: str, raw_parent: Path, target_dir: Path) -> Path | None:
    """Move minerU output from raw_parent/<name>/auto/ to target_dir/<name>/.

    minerU writes <name>/auto/<name>.md + images/ + image-map.txt + a pile
    of auxiliary _layout.pdf / _middle.json / _model.json / _origin.pdf /
    _span.pdf / _content_list*.json files. This function:

      * renames <name>.md → paper.md
      * moves images/ and image-map.txt up one level into target_dir/<name>/
      * rmtree's the entire auto/ directory (everything left is junk)
      * removes the raw_parent/<name>/ wrapper if it differs from
        target_dir/<name>/ and is now empty (single mode cleanup)

    Single mode: raw_parent ≠ target_dir → raw wrapper gets removed.
    Batch mode:  raw_parent == target_dir → paper_dir is the raw wrapper,
                 so the final rmdir is skipped.

    Returns the final paper.md path, or None if minerU produced no output.
    """
    auto_dir = raw_parent / name / "auto"
    if not auto_dir.is_dir():
        return None
    paper_dir = target_dir / name
    paper_dir.mkdir(parents=True, exist_ok=True)

    # 1. Move the markdown (renamed to paper.md). minerU produces a single
    # <name>.md; glob+first-hit guards against future variants.
    md_src = next(iter(auto_dir.glob("*.md")), None)
    if md_src is not None:
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

    # 3. Move image-map.txt (generated earlier by map_mineru_images.py).
    src_map = auto_dir / "image-map.txt"
    if src_map.exists():
        dst_map = paper_dir / "image-map.txt"
        dst_map.unlink(missing_ok=True)
        shutil.move(str(src_map), str(dst_map))

    # 4. Everything still in auto/ is minerU auxiliary output — drop it.
    shutil.rmtree(str(auto_dir), ignore_errors=True)

    # 5. Single mode: remove the now-empty raw wrapper. Batch mode skips
    # this because raw_root and paper_dir are the same directory.
    raw_root = raw_parent / name
    if raw_root != paper_dir and raw_root.is_dir() and not any(raw_root.iterdir()):
        raw_root.rmdir()

    return paper_dir / "paper.md"


# ---------------------------------------------------------------------------
# Single-paper mode
# ---------------------------------------------------------------------------

def parse_single(pdf_path: Path, output_dir: Path):
    """Parse a single PDF, standardize output, generate image map."""
    name = derive_name(str(pdf_path))
    raw_dir = output_dir / name  # minerU raw output (will be cleaned up)
    raw_dir.mkdir(parents=True, exist_ok=True)

    print(f"Parsing {pdf_path} ...")
    t0 = time.time()

    success = run_mineru(pdf_path, output_dir)
    if not success:
        print("  minerU failed", file=sys.stderr)
        sys.exit(1)

    # Image mapping (runs from raw minerU output before cleanup)
    img_result = generate_image_map(name, raw_dir)
    if not img_result["success"]:
        print(f"  image map failed: {img_result.get('error', 'unknown')}", file=sys.stderr)

    # Standardize: clean up minerU trash, move to parsed/<name>/
    paper_md = standardize_output(name, output_dir, output_dir / "parsed")
    std_dir = paper_md.parent  # output/parsed/<name>/
    images_dir = std_dir / "images"
    image_map = std_dir / "image-map.txt"

    elapsed = time.time() - t0
    print(f"Done ({elapsed:.0f}s)")
    print(f"  Markdown: {paper_md}")
    print(f"  Images:   {images_dir}")
    print(f"  Map:      {image_map}")
    print(f"  In slides: \\graphicspath{{{images_dir}/}} "
          f"+ \\includegraphics{{<hash>.jpg}}")


# ---------------------------------------------------------------------------
# Batch mode (same as original batch_parse.py logic)
# ---------------------------------------------------------------------------

def parse_batch(pdf_dir: Path, parsed_dir: Path, output_dir: Path, force: bool):
    """Batch parse all PDFs in a directory."""
    pdfs = sorted(pdf_dir.glob("*.pdf")) or sorted(pdf_dir.glob("*.PDF"))
    if not pdfs:
        print(f"No PDFs found in {pdf_dir}")
        sys.exit(1)

    # Stage 1: Scan
    new_papers = []
    skipped = []
    for pdf in pdfs:
        name = derive_name(str(pdf))
        output_md = parsed_dir / name / "paper.md"
        if output_md.exists() and not force:
            skipped.append((name, str(pdf)))
        else:
            new_papers.append((name, str(pdf)))

    print(f"Found {len(pdfs)} PDF(s): {len(new_papers)} new, {len(skipped)} skipped")
    for name, path in skipped:
        print(f"  SKIP  {name}: {path}")

    if not new_papers:
        print("Nothing to parse. Use --force to re-parse existing.")
        parsed_dir.mkdir(parents=True, exist_ok=True)
        manifest = {"settings": {"force": force}, "papers": []}
        with open(parsed_dir / "manifest.json", "w") as f:
            json.dump(manifest, f, indent=2)
        return

    # Stage 2: minerU batch parse
    parsed_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nParsing {len(new_papers)} PDF(s) with minerU native batch mode...")
    t0 = time.time()

    # Staging dir with symlinks using derived clean names
    tmpdir = tempfile.mkdtemp(prefix="beamer_batch_parse_")
    try:
        for name, pdf_path in new_papers:
            dst = os.path.join(tmpdir, name + ".pdf")
            os.symlink(os.path.abspath(pdf_path), dst)
        batch_success = run_mineru(Path(tmpdir), parsed_dir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    elapsed = time.time() - t0
    if batch_success:
        print(f"  minerU batch completed ({elapsed:.0f}s)")
    if not batch_success:
        print(f"  minerU batch failed ({elapsed:.0f}s), retrying individually...")
        for name, pdf_path in new_papers:
            paper_dir = parsed_dir / name
            if not (paper_dir / "auto").is_dir():
                print(f"  Retrying {name}...")
                run_mineru(Path(pdf_path), parsed_dir)
    # Image mapping (before cleanup, minerU output still intact)
    print("\nGenerating image maps...")
    for name, _ in new_papers:
        paper_raw = parsed_dir / name
        if paper_raw.is_dir():
            generate_image_map(name, paper_raw)

    # Standardize: clean up minerU trash per paper
    print("\nStandardizing output...")
    for name, _ in new_papers:
        standardize_output(name, parsed_dir, parsed_dir)

    # Manifest
    papers_manifest = []
    for name, pdf_path in new_papers:
        paper_dir = parsed_dir / name
        paper_md = paper_dir / "paper.md"
        images_dir = paper_dir / "images"
        image_map = paper_dir / "image-map.txt"
        ok = paper_md.exists()
        entry = {
            "name": name,
            "pdf_path": pdf_path,
            "parsed_dir": str(paper_dir),
            "paper_md": str(paper_md) if ok else None,
            "images_dir": str(images_dir) if images_dir.is_dir() else None,
            "image_map": str(image_map) if image_map.exists() else None,
            "status": "parsed" if ok else "failed",
            "error": None if ok else f"paper.md not found at {paper_md}",
        }
        papers_manifest.append(entry)


    manifest = {
        "settings": {
            "source_dir": str(pdf_dir),
            "output_dir": str(output_dir),
            "parsed_dir": str(parsed_dir),
            "force": force,
        },
        "papers": papers_manifest,
    }

    manifest_path = parsed_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\nManifest written to {manifest_path}")

    n_ok = sum(1 for p in papers_manifest if p["status"] == "parsed")
    n_fail = sum(1 for p in papers_manifest if p["status"] != "parsed")
    total = time.time() - t0
    print(f"\nDone: {n_ok} parsed, {n_fail} failed, {len(skipped)} skipped "
          f"({total:.0f}s)")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="minerU wrapper: single PDF or batch parsing with env setup"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--single", metavar="PDF", help="Parse a single PDF")
    mode.add_argument("--batch", metavar="DIR", help="Parse all PDFs in a directory")
    parser.add_argument("output", nargs="?", default=".",
                        help="Output root directory (default: current directory)")
    parser.add_argument("--force", action="store_true",
                        help="Re-parse even if output exists (batch only)")
    args = parser.parse_args()

    if args.single:
        pdf = Path(args.single)
        if not pdf.is_file():
            print(f"Error: {pdf} is not a file", file=sys.stderr)
            sys.exit(1)
        if not mineru_available():
            print("Warning: minerU conda env (torch_rocm72) not found.",
                  file=sys.stderr)
        parse_single(pdf, Path(args.output))
    else:
        pdf_dir = Path(args.batch)
        if not pdf_dir.is_dir():
            print(f"Error: {pdf_dir} is not a directory", file=sys.stderr)
            sys.exit(1)
        if not mineru_available():
            print("Warning: minerU conda env (torch_rocm72) not found.",
                  file=sys.stderr)
        output_dir = Path(args.output)
        parse_batch(pdf_dir, output_dir / "parsed", output_dir, args.force)


if __name__ == "__main__":
    main()
