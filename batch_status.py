#!/usr/bin/env python3
"""Print a formatted status summary for a batch_parse.py run.

Usage:
    python3 batch_status.py /mnt/shared/batch_out/parsed/
    watch -n 5 python3 batch_status.py /mnt/shared/batch_out/parsed/
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m {seconds % 60:.0f}s"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m"


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <parsed_dir>", file=sys.stderr)
        sys.exit(1)

    parsed_dir = Path(sys.argv[1])
    pf = parsed_dir / "progress.json"
    if not pf.exists():
        print(f"No progress.json found in {parsed_dir}")
        sys.exit(1)

    progress = json.loads(pf.read_text())
    files = progress.get("files", {})

    total = len(files)
    done_entries = {k: v for k, v in files.items() if v.get("status") == "done"}
    failed_entries = {k: v for k, v in files.items() if v.get("status") == "failed"}
    pending_entries = {k: v for k, v in files.items() if v.get("status") == "pending"}
    done = len(done_entries)
    failed = len(failed_entries)
    pending = len(pending_entries)
    completed = done + failed

    pct = (completed / total * 100) if total > 0 else 0

    started_at = progress.get("started_at", "?")
    updated_at = progress.get("updated_at", "?")

    try:
        t0 = datetime.fromisoformat(started_at)
        t1 = datetime.fromisoformat(updated_at)
        elapsed = (t1 - t0).total_seconds()
    except Exception:
        elapsed = 0

    if elapsed > 0 and completed > 0:
        rate = completed / elapsed
        if rate > 0:
            remaining = (total - completed) / rate
            eta = fmt_duration(remaining)
        else:
            eta = "?"
    else:
        rate = 0
        eta = "?"

    # Average time per successful parse
    times = [v.get("time", 0) for v in done_entries.values() if v.get("time")]
    avg_time = sum(times) / len(times) if times else 0

    # Progress bar
    bar_width = 40
    filled = int(bar_width * completed / total) if total > 0 else 0
    bar = "\u2588" * filled + "\u2591" * (bar_width - filled)

    print()
    print(f"  Source:   {progress.get('source_dir', '?')}")
    print(f"  Server:   {progress.get('server_url', '?')}")
    print()
    print(f"  [{bar}]  {pct:.1f}%")
    print()
    print(f"  Total:    {total}")
    print(f"  Done:     {done}")
    print(f"  Failed:   {failed}")
    print(f"  Pending:  {pending}")
    print()
    print(f"  Elapsed:  {fmt_duration(elapsed)}")
    print(f"  ETA:      {eta}")
    print(f"  Rate:     {rate:.3f} req/s")
    print(f"  Avg time: {avg_time:.1f}s per PDF")
    print()

    # Recent failures
    if failed_entries:
        print("  Recent failures:")
        for name, entry in sorted(failed_entries.items())[-10:]:
            err = entry.get("error", "?")[:80]
            retries = entry.get("retries", "?")
            print(f"    {name}  retries={retries}  {err}")

    # Last 5 completed
    if done_entries:
        print()
        print("  Last completed:")
        for name, entry in sorted(done_entries.items())[-5:]:
            t = entry.get("time", "?")
            imgs = entry.get("images_count", "?")
            print(f"    {name}  {t}s  {imgs} images")


if __name__ == "__main__":
    main()