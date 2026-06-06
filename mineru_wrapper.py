#!/usr/bin/env python3
"""minerU wrapper — parse PDFs with automated ROCm env and output standardization.

Usage:
    mineru_wrapper.py paper.pdf                     # single PDF
    mineru_wrapper.py pdf_dir/                      # all PDFs in dir
    mineru_wrapper.py pdf_dir/ extra.pdf            # mixed
    mineru_wrapper.py paper.pdf --force             # re-parse

Logs: ~/logs/mineru/run_<timestamp>.log (full stdout + stderr per run)
Output: parsed/<name>/{paper.md, images/, image-map.txt}

For batch (2+ PDFs), also writes parsed/manifest.json.
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

def collect_pdfs(paths: list[str]) -> list[tuple[str, str]]:
    """Resolve paths to (name, abs_path) for every PDF found.

    Each path is either a .pdf file or a directory (scanned for *.pdf).
    """
    pdfs = []
    seen = set()
    for p in paths:
        path = Path(p).resolve()
        if path.is_file() and path.suffix.lower() == ".pdf":
            name = derive_name(str(path))
            if name not in seen:
                pdfs.append((name, str(path)))
                seen.add(name)
        elif path.is_dir():
            for f in sorted(path.glob("*.pdf")) + sorted(path.glob("*.PDF")):
                name = derive_name(str(f))
                if name not in seen:
                    pdfs.append((name, str(f)))
                    seen.add(name)
        else:
            print(f"Warning: {p} is not a PDF file or directory, skipping", file=sys.stderr)
    return pdfs


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
def main():
    parser = argparse.ArgumentParser(
        description="minerU wrapper — parse PDFs with automated ROCm env and output standardization"
    )
    parser.add_argument("pdfs", nargs="+", metavar="PATH",
                        help="PDF file(s) or director(ies) of PDFs")
    parser.add_argument("--force", action="store_true",
                        help="Re-parse already-processed PDFs")
    parser.add_argument("output", nargs="?", default=".",
                        help="Output root directory (default: current directory)")
    args = parser.parse_args()

    all_pdfs = collect_pdfs(args.pdfs)
    if not all_pdfs:
        print("No PDFs found in the given paths", file=sys.stderr)
        sys.exit(1)

    if not mineru_available():
        print("Warning: minerU conda env (torch_rocm72) not found.",
              file=sys.stderr)

    output_dir = Path(args.output)
    is_batch = len(all_pdfs) > 1
    t0 = time.time()

    # Decide which PDFs to parse
    if not is_batch and not args.force:
        papers = all_pdfs
    else:
        papers = [(n, p) for n, p in all_pdfs
                  if args.force or not (output_dir / "parsed" / n / "paper.md").exists()]
        skipped = len(all_pdfs) - len(papers)
        for n, p in all_pdfs:
            if (n, p) not in papers:
                print(f"  SKIP  {n}: {p}")

    if not papers:
        print("All PDFs already parsed. Use --force to re-parse.")
        return

    # Run minerU
    if is_batch:
        print(f"\nParsing {len(papers)} PDF(s) via batch mode...")
        tmpdir = tempfile.mkdtemp(prefix="mineru_")
        try:
            for name, pdf_path in papers:
                dst = os.path.join(tmpdir, name + ".pdf")
                os.symlink(os.path.abspath(pdf_path), dst)
            batch_ok = run_mineru(Path(tmpdir), output_dir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        if not batch_ok:
            print("  minerU batch failed, retrying individually...")
            for name, pdf_path in papers:
                if not (output_dir / name / "auto").is_dir():
                    print(f"  Retrying {name}...")
                    run_mineru(Path(pdf_path), output_dir)
    else:
        name, pdf_path = papers[0]
        print(f"\nParsing {Path(pdf_path).name}...")
        if not run_mineru(Path(pdf_path), output_dir):
            print("  minerU failed", file=sys.stderr)
            sys.exit(1)

    # Common post-processing
    print("\nPost-processing...")
    for name, pdf_path in papers:
        raw_dir = output_dir / name
        if raw_dir.is_dir():
            generate_image_map(name, raw_dir)
            standardize_output(name, output_dir, output_dir / "parsed")

    elapsed = time.time() - t0

    # Output
    if is_batch:
        manifest = {
            "settings": {
                "source": args.pdfs,
                "output_dir": str(output_dir),
                "force": args.force,
            },
            "papers": [
                {
                    "name": n,
                    "pdf_path": p,
                    "paper_md": str(output_dir / "parsed" / n / "paper.md"),
                    "status": "parsed" if (output_dir / "parsed" / n / "paper.md").exists() else "failed",
                }
                for n, p in papers
            ],
        }
        manifest_path = output_dir / "parsed" / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
        n_ok = sum(1 for e in manifest["papers"] if e["status"] == "parsed")
        n_fail = len(manifest["papers"]) - n_ok
        print(f"\nManifest: {manifest_path}")
        print(f"Done: {n_ok} parsed, {n_fail} failed, {skipped} skipped ({elapsed:.0f}s)")
    else:
        name = papers[0][0]
        print(f"\nDone ({elapsed:.0f}s)")
        print(f"  Markdown: {output_dir}/parsed/{name}/paper.md")
        print(f"  Images:   {output_dir}/parsed/{name}/images/")
        print(f"  Map:      {output_dir}/parsed/{name}/image-map.txt")


if __name__ == "__main__":
    main()