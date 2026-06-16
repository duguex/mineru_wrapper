#!/usr/bin/env python3
"""Batch minerU parser via router API — concurrent, checkpointed, with retry.

Sends PDFs one-by-one to a mineru-router (or mineru-api) server, saves
paper.md + images/, runs map_mineru_images.py for figure labels, cleans
orphan images, and maintains a progress.json checkpoint for resume.

Usage:
    python3 batch_parse.py --src /path/to/pdfs \
        --output /mnt/shared/batch_out \
        --url http://127.0.0.1:8002 \
        --concurrency 4

    # Resume interrupted run (same command)
    python3 batch_parse.py --src /path/to/pdfs \
        --output /mnt/shared/batch_out \
        --url http://127.0.0.1:8002 \
        --concurrency 4

Progress:  watch -n 5 python3 batch_status.py <output>/parsed/
"""

import argparse
import asyncio
import base64
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
MAP_SCRIPT = HERE / "map_mineru_images.py"

# Orphan filter regex — same as mineru_wrapper.py:standardize_output
MD_IMAGE_RE = re.compile(r'\(images/(\S+\.jpg)')


def load_progress(parsed_dir: Path) -> dict:
    """Load progress.json or return empty skeleton."""
    pf = parsed_dir / "progress.json"
    if pf.exists():
        return json.loads(pf.read_text())
    return {"files": {}}


def save_progress(parsed_dir: Path, progress: dict):
    """Atomic write progress.json."""
    tmp = parsed_dir / "progress.json.tmp"
    progress["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp.write_text(json.dumps(progress, ensure_ascii=False, indent=2))
    tmp.rename(parsed_dir / "progress.json")


def list_pending(src_dir: Path, progress: dict, max_retries: int) -> list[Path]:
    """Return PDFs that are not yet successfully processed or permanently failed."""
    all_pdfs = sorted(src_dir.glob("*.pdf")) + sorted(src_dir.glob("*.PDF"))
    files_state = progress.get("files", {})
    pending = []
    for pdf in all_pdfs:
        name = pdf.stem
        st = files_state.get(name, {})
        status = st.get("status", "pending")
        retries = st.get("retries", 0)
        if status == "done":
            continue
        if status == "failed" and retries >= max_retries:
            continue
        pending.append(pdf)
    return pending


def update_file_state(progress: dict, name: str, updates: dict):
    """Merge updates into progress['files'][name]."""
    files = progress.setdefault("files", {})
    entry = files.setdefault(name, {"status": "pending", "retries": 0})
    entry.update(updates)


async def submit_one(
    client: httpx.AsyncClient,
    base_url: str,
    pdf_path: Path,
) -> dict:
    """POST a PDF to /file_parse, return the JSON response or error dict."""
    url = f"{base_url}/file_parse"
    try:
        with open(pdf_path, "rb") as fh:
            files_data = {"files": (pdf_path.name, fh, "application/pdf")}
            data = {
                "backend": "pipeline",
                "parse_method": "auto",
                "lang_list": ["en"],
                "return_md": "true",
                "return_images": "true",
            }
            resp = await client.post(url, files=files_data, data=data)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    if resp.status_code == 200:
        return {"ok": True, "data": resp.json()}
    return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:300]}"}


def save_paper_output(paper_dir: Path, response_data: dict) -> dict:
    """Write paper.md and images/ from API response. Returns {images_count}."""
    results = response_data.get("results", {})
    images_count = 0
    for pdf_stem, file_results in results.items():
        md_content = file_results.get("md_content", "")
        if md_content:
            (paper_dir / "paper.md").write_text(md_content, encoding="utf-8")

        images = file_results.get("images", {})
        if images:
            img_dir = paper_dir / "images"
            img_dir.mkdir(parents=True, exist_ok=True)
            for name, b64data in images.items():
                try:
                    raw = b64data.split(",", 1)[-1]
                    (img_dir / name).write_bytes(base64.b64decode(raw))
                    images_count += 1
                except Exception:
                    pass
    return {"images_count": images_count}


def generate_image_map(paper_dir: Path) -> dict:
    """Run map_mineru_images.py to produce image-map.txt."""
    md = paper_dir / "paper.md"
    if not md.exists():
        return {"ok": False, "error": "paper.md missing"}
    out = paper_dir / "image-map.txt"
    result = subprocess.run(
        [sys.executable, str(MAP_SCRIPT), "-m", str(md), "-o", str(out)],
        capture_output=True, text=True, timeout=120,
    )
    return {
        "ok": result.returncode == 0,
        "error": result.stderr.strip() if result.returncode != 0 else None,
    }


def clean_orphan_images(paper_dir: Path):
    """Remove JPGs in images/ not referenced by image-map.txt or paper.md."""
    md_path = paper_dir / "paper.md"
    map_path = paper_dir / "image-map.txt"
    img_dir = paper_dir / "images"
    if not img_dir.is_dir():
        return

    mapped = set()
    if map_path.exists():
        for line in map_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                fname = line.split("\u2192", 1)[0].strip()
                if fname:
                    mapped.add(fname)

    md_refs = set()
    if md_path.exists():
        md_refs = set(MD_IMAGE_RE.findall(md_path.read_text()))

    for f in list(img_dir.glob("*.jpg")):
        if f.name not in mapped and f.name not in md_refs:
            f.unlink()


def write_manifest(parsed_dir: Path, progress: dict):
    """Write manifest.json summarizing the run."""
    files = progress.get("files", {})
    manifest = {
        "settings": {
            "source_dir": progress.get("source_dir", "?"),
            "output_dir": str(parsed_dir),
            "started_at": progress.get("started_at", "?"),
            "updated_at": progress.get("updated_at", "?"),
        },
        "summary": {
            "total": progress.get("total", 0),
            "done": sum(1 for e in files.values() if e.get("status") == "done"),
            "failed": sum(1 for e in files.values() if e.get("status") == "failed"),
            "pending": sum(1 for e in files.values() if e.get("status") == "pending"),
        },
        "papers": [
            {
                "name": name,
                "status": entry.get("status"),
                "paper_md": str(parsed_dir / name / "paper.md"),
                "time": entry.get("time"),
                "images": entry.get("images_count"),
                "error": entry.get("error"),
                "retries": entry.get("retries"),
            }
            for name, entry in sorted(files.items())
        ],
    }
    (parsed_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2)
    )


async def process_one(
    client: httpx.AsyncClient,
    base_url: str,
    pdf_path: Path,
    parsed_dir: Path,
    progress: dict,
    progress_lock: asyncio.Lock,
    stats: dict,
    stats_lock: asyncio.Lock,
    max_retries: int,
    retry_delay: float,
):
    """Process a single PDF: submit → save → image-map → clean → checkpoint."""
    name = pdf_path.stem
    paper_dir = parsed_dir / name
    retry = progress.get("files", {}).get(name, {}).get("retries", 0)

    while retry <= max_retries:
        t0 = time.monotonic()

        resp = await submit_one(client, base_url, pdf_path)

        if resp["ok"]:
            elapsed = time.monotonic() - t0
            paper_dir.mkdir(parents=True, exist_ok=True)
            save_result = save_paper_output(paper_dir, resp["data"])

            img_map_result = generate_image_map(paper_dir)
            if not img_map_result["ok"]:
                elapsed_str = f"{elapsed:.1f}s (image-map failed: {img_map_result['error']})"
            else:
                clean_orphan_images(paper_dir)
                elapsed_str = f"{elapsed:.1f}s"

            async with progress_lock:
                update_file_state(progress, name, {
                    "status": "done",
                    "time": round(elapsed, 1),
                    "images_count": save_result["images_count"],
                    "retries": retry,
                })
                progress["completed"] = progress.get("completed", 0) + 1
                save_progress(parsed_dir, progress)

            async with stats_lock:
                stats["ok"] += 1
                stats["total_time"] += elapsed
                seq = stats["ok"] + stats["failed"]

            print(f"  [{seq:>5}/{progress['total']}] {name}  OK  {elapsed_str}", flush=True)
            return

        retry += 1
        if retry <= max_retries:
            wait = retry_delay * retry
            async with stats_lock:
                seq = stats["ok"] + stats["failed"] + 1
            print(
                f"  [{seq:>5}/{progress['total']}] {name}  RETRY {retry}/{max_retries} "
                f"({resp['error'][:80]})  waiting {wait:.0f}s...",
                flush=True,
            )
            async with progress_lock:
                update_file_state(progress, name, {"retries": retry, "error": resp["error"]})
                save_progress(parsed_dir, progress)
            await asyncio.sleep(wait)
        else:
            async with progress_lock:
                update_file_state(progress, name, {
                    "status": "failed",
                    "retries": retry,
                    "error": resp["error"],
                })
                progress["completed"] = progress.get("completed", 0) + 1
                save_progress(parsed_dir, progress)

            async with stats_lock:
                stats["failed"] += 1
                seq = stats["ok"] + stats["failed"]
            print(
                f"  [{seq:>5}/{progress['total']}] {name}  FAILED (after {retries} retries): "
                f"{resp['error'][:120]}",
                flush=True,
            )
            return


async def run_batch(
    src_dir: Path,
    parsed_dir: Path,
    base_url: str,
    concurrency: int,
    max_retries: int,
    retry_delay: float,
):
    """Main batch processing loop."""
    src_dir = src_dir.resolve()
    parsed_dir = parsed_dir.resolve()
    parsed_dir.mkdir(parents=True, exist_ok=True)

    progress = load_progress(parsed_dir)
    all_pdfs = sorted(src_dir.glob("*.pdf")) + sorted(src_dir.glob("*.PDF"))

    for pdf in all_pdfs:
        name = pdf.stem
        if name not in progress.get("files", {}):
            update_file_state(progress, name, {"status": "pending", "retries": 0})

    progress.setdefault("source_dir", str(src_dir))
    progress.setdefault("output_dir", str(parsed_dir))
    progress.setdefault("server_url", base_url)
    progress.setdefault("started_at", datetime.now(timezone.utc).isoformat())
    progress["total"] = len(all_pdfs)
    progress["completed"] = sum(
        1 for e in progress.get("files", {}).values() if e.get("status") == "done"
    )
    save_progress(parsed_dir, progress)

    pending = list_pending(src_dir, progress, max_retries)
    done_count = progress["completed"]
    fail_count = sum(
        1 for e in progress.get("files", {}).values()
        if e.get("status") == "failed"  and e.get("retries", 0) >= max_retries
    )
    skip_count = done_count + fail_count

    print(f"Source:   {src_dir}")
    print(f"Output:   {parsed_dir}")
    print(f"Server:   {base_url}")
    print(f"Total:    {len(all_pdfs)}  Done: {done_count}  Failed: {fail_count}  "
          f"Pending: {len(pending)}")
    print(f"Concurrency: {concurrency}  Max retries: {max_retries}")
    print()

    if not pending:
        print("All PDFs processed.")
        write_manifest(parsed_dir, progress)
        return

    stats = {"ok": 0, "failed": 0, "total_time": 0.0}
    progress_lock = asyncio.Lock()
    stats_lock = asyncio.Lock()
    sem = asyncio.Semaphore(concurrency)

    async def bounded(pdf_path: Path):
        async with sem:
            await process_one(
                client, base_url, pdf_path, parsed_dir,
                progress, progress_lock, stats, stats_lock,
                max_retries, retry_delay,
            )

    t_start = time.monotonic()
    timeout = httpx.Timeout(900.0, connect=30.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        tasks = [asyncio.create_task(bounded(p)) for p in pending]
        await asyncio.gather(*tasks, return_exceptions=True)

    elapsed_total = time.monotonic() - t_start

    final_done = sum(
        1 for e in progress.get("files", {}).values() if e.get("status") == "done"
    )
    final_failed = sum(
        1 for e in progress.get("files", {}).values() if e.get("status") == "failed"
    )

    print()
    print(f"Done:    {final_done} parsed, {final_failed} failed "
          f"({elapsed_total:.0f}s total, "
          f"{len(pending) / elapsed_total:.3f} req/s)")
    write_manifest(parsed_dir, progress)


def main():
    parser = argparse.ArgumentParser(
        description="Batch minerU parser via router API with checkpoint/resume"
    )
    parser.add_argument("--src", required=True,
                        help="Directory containing PDF files")
    parser.add_argument("--output", required=True,
                        help="Output root directory (parsed/ created inside)")
    parser.add_argument("--url", required=True,
                        help="minerU server base URL (e.g. http://127.0.0.1:8002)")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="Max concurrent requests (default: 4)")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="Max retries per PDF (default: 3)")
    parser.add_argument("--retry-delay", type=float, default=15.0,
                        help="Base retry delay seconds, multiplied by attempt (default: 15)")
    args = parser.parse_args()

    src_dir = Path(args.src)
    if not src_dir.is_dir():
        print(f"Error: --src must be a directory: {src_dir}", file=sys.stderr)
        sys.exit(1)

    parsed_dir = Path(args.output) / "parsed"

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            run_batch(
                src_dir, parsed_dir, args.url.rstrip("/"),
                args.concurrency, args.max_retries, args.retry_delay,
            )
        )
    except KeyboardInterrupt:
        print("\nInterrupted. Progress saved — re-run the same command to resume.")
    finally:
        loop.close()


if __name__ == "__main__":
    main()