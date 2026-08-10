#!/usr/bin/env python3
"""minerU wrapper — parse PDFs with automated CUDA env and output standardization.

Usage:
    mineru_wrapper.py paper.pdf                      # single PDF
    mineru_wrapper.py pdf_dir/                       # all PDFs in dir
    mineru_wrapper.py pdf_dir/ extra.pdf             # mixed (files + dirs)
    mineru_wrapper.py paper.pdf --force              # re-parse
    mineru_wrapper.py paper.pdf -o /tmp/out          # custom output root

Logs: ~/logs/mineru/run_<timestamp>.log (full stdout + stderr per run)
Output: <output>/parsed/<name>/{paper.md, images/, image-map.txt}

For batch (2+ PDFs), also writes <output>/parsed/manifest.json.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from finalize import finalize_output

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
        ["conda", "run", "-n", "mineru", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0


def run_mineru(input_path: Path, output_dir: Path, gpu: str = "0") -> bool:
    """Run minerU with persistent logging. Output streams to both terminal and log file."""
    log_dir = Path.home() / "logs" / "mineru"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    env_script = os.path.expanduser("~/mineru-cuda/mineru-cuda-env.sh")
    cmd = (
        f"export MINERU_API_MAX_CONCURRENT_REQUESTS=1 && "
        f"source {shlex.quote(env_script)} && "
        f"export CUDA_VISIBLE_DEVICES={gpu} && "
        f"export PATH=/opt/conda/bin:$PATH && "
        f"conda run -n mineru mineru -p {shlex.quote(str(input_path))} "
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


def main():
    parser = argparse.ArgumentParser(
        description="minerU wrapper — parse PDFs with automated CUDA env and output standardization"
    )
    parser.add_argument("pdfs", nargs="+", metavar="PATH",
                        help="PDF file(s) or director(ies) of PDFs")
    parser.add_argument("-o", "--output", default=".",
                        help="Output root directory (default: current directory)")
    parser.add_argument("--force", action="store_true",
                        help="Re-parse already-processed PDFs")
    parser.add_argument("--gpus", default="0",
                        help="GPU(s) to use, CSV e.g. '0' or '0,1' (default: 0)")
    args = parser.parse_args()

    all_pdfs = collect_pdfs(args.pdfs)
    if not all_pdfs:
        print("No PDFs found in the given paths", file=sys.stderr)
        sys.exit(1)

    if not mineru_available():
        print("Warning: minerU conda env (mineru) not found.",
              file=sys.stderr)

    output_dir = Path(args.output)
    t0 = time.time()

    # Skip already-parsed unless --force.
    papers = [(n, p) for n, p in all_pdfs
              if args.force or not (output_dir / "parsed" / n / "paper.md").exists()]
    papers_set = set(papers)
    skipped = [(n, p) for n, p in all_pdfs if (n, p) not in papers_set]
    for n, p in skipped:
        print(f"  SKIP  {n}: {p}")

    if not papers:
        print("All PDFs already parsed. Use --force to re-parse.")
        return

    # Run minerU — stage PDFs as symlinks under derived names so minerU's
    # output dir matches what post-processing expects (avoids the PDF-stem
    # vs derived-name mismatch that breaks special-character filenames).
    # minerU accepts a directory as input regardless of how many PDFs are
    # in it, so single (N=1) is just batch with one symlink.
    # Multi-GPU: split papers evenly, stage per GPU in separate tmpdirs,
    # run mineru processes in parallel via ThreadPoolExecutor.
    gpus = [g.strip() for g in args.gpus.split(",")]
    gpu_groups = []
    for i, gpu in enumerate(gpus):
        chunk = papers[i::len(gpus)]
        if chunk:
            gpu_groups.append((gpu, chunk))

    print(f"\nParsing {len(papers)} PDF(s) on {args.gpus}...")
    from concurrent.futures import ThreadPoolExecutor, as_completed

    with tempfile.TemporaryDirectory(prefix="mineru_") as tmpdir_root:
        tmpdir_root = Path(tmpdir_root)

        def _run_gpu(gpu: str, chunk: list[tuple[str, str]]):
            tmpdir = tmpdir_root / f"gpu{gpu}"
            tmpdir.mkdir()
            staged = []
            for name, src_path in chunk:
                dst = tmpdir / f"{name}.pdf"
                os.symlink(os.path.abspath(src_path), dst)
                staged.append((name, dst))

            ok = run_mineru(tmpdir, output_dir, gpu)
            if not ok:
                for name, staged_pdf in staged:
                    if not (output_dir / name / "auto").is_dir():
                        print(f"  Retrying {name}...")
                        run_mineru(staged_pdf, output_dir, gpu)
            return ok

        if len(gpu_groups) == 1:
            gpu, chunk = gpu_groups[0]
            _run_gpu(gpu, chunk)
        else:
            with ThreadPoolExecutor(max_workers=len(gpu_groups)) as ex:
                futures = {ex.submit(_run_gpu, gpu, chunk): gpu
                           for gpu, chunk in gpu_groups}
                for f in as_completed(futures):
                    gpu = futures[f]
                    try:
                        f.result()
                    except Exception as e:
                        print(f"  GPU {gpu} failed: {e}", file=sys.stderr)

    # Post-processing — one finalizer relocates minerU's raw tree, generates
    # image-map.txt in-process, drops orphan images, and clears auxiliary junk.
    print("\nPost-processing...")
    for name, _ in papers:
        raw_dir = output_dir / name
        if (raw_dir / "auto").is_dir():
            finalize_output(name, output_dir, output_dir / "parsed")

    elapsed = time.time() - t0

    # Always write manifest (single-PDF runs also benefit from a failure
    # record that survives the process exit).
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
    print(f"Done: {n_ok} parsed, {n_fail} failed, {len(skipped)} skipped ({elapsed:.0f}s)")
    # Friendly path printout for the common single-PDF case.
    if len(papers) == 1:
        name = papers[0][0]
        print(f"  Markdown: {output_dir}/parsed/{name}/paper.md")
        print(f"  Images:   {output_dir}/parsed/{name}/images/")
        print(f"  Map:      {output_dir}/parsed/{name}/image-map.txt")


if __name__ == "__main__":
    main()