#!/usr/bin/env python3
"""Batch minerU parser via router API — concurrent, checkpointed, with retry.

Sends PDFs one-by-one to a mineru-router (or mineru-api) server, saves
paper.md + images/, then finalizes each result in-process (image-map +
orphan cleanup) via finalize.py, and maintains a progress.json checkpoint
for resume.

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
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from bookkeeping import (
    derive_name,
    is_skipped,
    manifest_payload,
    paper_md_path,
    paper_row,
)
from finalize import finalize_output
from parse_client import (
    TIMEOUTS,
    ParseClientError,
    file_parse_async,
    parse_params,
)

# Long unattended jobs use the table's batch budget; connect stays tight so a
# dead server is noticed quickly (a bare float would widen connect to the
# full parse budget).
BATCH_PARSE_TIMEOUT = httpx.Timeout(TIMEOUTS["parse_batch"], connect=30.0)


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
    """Return PDFs to submit: not done, not skipped, not permanently failed."""
    all_pdfs = sorted(src_dir.glob("*.pdf")) + sorted(src_dir.glob("*.PDF"))
    files_state = progress.get("files", {})
    pending = []
    for pdf in all_pdfs:
        name = derive_name(str(pdf))
        st = files_state.get(name, {})
        status = st.get("status", "pending")
        retries = st.get("retries", 0)
        if status in ("done", "skipped"):
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


def write_manifest(parsed_dir: Path, progress: dict):
    """Write manifest.json summarizing the run (glossary status vocabulary)."""
    files = progress.get("files", {})
    rows = []
    for name, entry in sorted(files.items()):
        # progress.json speaks its own transient vocabulary; the manifest
        # speaks the glossary. Anything that never produced an artifact —
        # including entries still "pending" because an exception was
        # swallowed — is failed, so summary always sums to total.
        st = entry.get("status", "pending")
        if st == "done":
            status = "parsed"
        elif st == "skipped":
            status = "skipped"
        else:
            status = "failed"
        rows.append(paper_row(
            name,
            entry.get("pdf_path", "?"),
            str(paper_md_path(parsed_dir, name)),
            status,
            time=entry.get("time"),
            images=entry.get("images_count"),
            error=entry.get("error"),
            retries=entry.get("retries"),
        ))
    manifest = manifest_payload({
        "source_dir": progress.get("source_dir", "?"),
        "output_dir": str(parsed_dir),
        "started_at": progress.get("started_at", "?"),
        "updated_at": progress.get("updated_at", "?"),
    }, rows)
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
    params: dict,
):
    """Process a single PDF: submit → save → image-map → clean → checkpoint."""
    name = derive_name(str(pdf_path))
    paper_dir = parsed_dir / name
    retry = progress.get("files", {}).get(name, {}).get("retries", 0)

    while retry <= max_retries:
        t0 = time.monotonic()

        try:
            data = await file_parse_async(
                client, base_url, [pdf_path], params, timeout=BATCH_PARSE_TIMEOUT)
            ok, error = True, None
        except ParseClientError as e:
            ok, error = False, str(e)

        if ok:
            elapsed = time.monotonic() - t0
            paper_dir.mkdir(parents=True, exist_ok=True)
            save_result = save_paper_output(paper_dir, data)

            # Finalize in-process: image-map (non-fatal on failure) + orphan
            # cleanup; orphans are kept when paper.md references them.
            finalize_output(name, parsed_dir, parsed_dir)
            if not (paper_dir / "image-map.txt").exists():
                elapsed_str = f"{elapsed:.1f}s (no image-map)"
            else:
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
                f"({error[:80]})  waiting {wait:.0f}s...",
                flush=True,
            )
            async with progress_lock:
                update_file_state(progress, name, {"retries": retry, "error": error})
                save_progress(parsed_dir, progress)
            await asyncio.sleep(wait)
        else:
            async with progress_lock:
                update_file_state(progress, name, {
                    "status": "failed",
                    "retries": retry,
                    "error": error,
                })
                progress["completed"] = progress.get("completed", 0) + 1
                save_progress(parsed_dir, progress)

            async with stats_lock:
                stats["failed"] += 1
                seq = stats["ok"] + stats["failed"]
            print(
                f"  [{seq:>5}/{progress['total']}] {name}  FAILED (after {retry} retries): "
                f"{error[:120]}",
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
    params: dict,
):
    """Main batch processing loop."""
    src_dir = src_dir.resolve()
    parsed_dir = parsed_dir.resolve()
    parsed_dir.mkdir(parents=True, exist_ok=True)

    progress = load_progress(parsed_dir)
    all_pdfs = sorted(src_dir.glob("*.pdf")) + sorted(src_dir.glob("*.PDF"))
    # Dedupe by derived name, mirroring the wrapper's collect_pdfs: two files
    # collapsing to one key would clobber each other's output.
    seen_names = set()
    deduped = []
    for pdf in all_pdfs:
        name = derive_name(str(pdf))
        if name in seen_names:
            print(f"Warning: {pdf.name} collides with another PDF's derived "
                  f"name ({name}), skipping", file=sys.stderr)
            continue
        seen_names.add(name)
        deduped.append(pdf)
    all_pdfs = deduped

    for pdf in all_pdfs:
        name = derive_name(str(pdf))
        if name not in progress.get("files", {}):
            update_file_state(progress, name, {
                "status": "pending", "retries": 0, "pdf_path": str(pdf),
            })

    # Glossary skip key: an existing artifact means already parsed. A paper
    # already "done" by this batch keeps its state (history preserved); any
    # other paper with an artifact is recorded as skipped and never re-queued.
    for pdf in all_pdfs:
        name = derive_name(str(pdf))
        if is_skipped(parsed_dir, name) and \
                progress.get("files", {}).get(name, {}).get("status") != "done":
            update_file_state(progress, name, {"status": "skipped"})

    progress.setdefault("source_dir", str(src_dir))
    progress.setdefault("output_dir", str(parsed_dir))
    progress.setdefault("server_url", base_url)
    progress.setdefault("started_at", datetime.now(timezone.utc).isoformat())
    progress["total"] = len(all_pdfs)
    save_progress(parsed_dir, progress)

    pending = list_pending(src_dir, progress, max_retries)
    files = progress.get("files", {})
    done_count = sum(1 for e in files.values() if e.get("status") == "done")
    fail_count = sum(
        1 for e in files.values()
        if e.get("status") == "failed" and e.get("retries", 0) >= max_retries
    )
    skipped_count = sum(1 for e in files.values() if e.get("status") == "skipped")

    print(f"Source:   {src_dir}")
    print(f"Output:   {parsed_dir}")
    print(f"Server:   {base_url}")
    print(f"Total:    {len(all_pdfs)}  Done: {done_count}  Failed: {fail_count}  "
          f"Skipped: {skipped_count}  Pending: {len(pending)}")
    print(f"Concurrency: {concurrency}  Max retries: {max_retries}")
    print()

    if not pending:
        print("All PDFs processed (or skipped by artifact).")
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
                max_retries, retry_delay, params,
            )

    t_start = time.monotonic()

    async with httpx.AsyncClient() as client:
        tasks = [asyncio.create_task(bounded(p)) for p in pending]
        await asyncio.gather(*tasks, return_exceptions=True)

    elapsed_total = time.monotonic() - t_start

    final_done = sum(
        1 for e in progress.get("files", {}).values() if e.get("status") == "done"
    )
    final_failed = sum(
        1 for e in progress.get("files", {}).values() if e.get("status") == "failed"
    )
    final_skipped = sum(
        1 for e in progress.get("files", {}).values() if e.get("status") == "skipped"
    )

    print()
    print(f"Done:    {final_done} parsed, {final_failed} failed, "
          f"{final_skipped} skipped ({elapsed_total:.0f}s total, "
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
    parser.add_argument("--lang", default="en", help="OCR language (default: en)")
    parser.add_argument("--no-formula", action="store_true", help="Disable formula parsing")
    parser.add_argument("--no-table", action="store_true", help="Disable table parsing")
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
                parse_params(lang=args.lang, formula=not args.no_formula,
                             table=not args.no_table),
            )
        )
    except KeyboardInterrupt:
        print("\nInterrupted. Progress saved — re-run the same command to resume.")
    finally:
        loop.close()


if __name__ == "__main__":
    main()