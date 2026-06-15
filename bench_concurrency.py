#!/usr/bin/env python3
"""GPU concurrency benchmark for mineru-api vs mineru-router.

Usage:
    python3 bench_concurrency.py <pdf_dir> <base_url> [--concurrency N]

Outputs a markdown row suitable for the comparison table.
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx


async def submit_one(client: httpx.AsyncClient, base_url: str, pdf_path: Path, idx: int) -> dict:
    """Send one PDF to the /file_parse endpoint, return timing dict."""
    url = f"{base_url}/file_parse"
    pdf_bytes = pdf_path.read_bytes()
    files = {"files": (pdf_path.name, pdf_bytes, "application/pdf")}
    data = {
        "lang": "en",
        "backend": "pipeline",
        "parse_method": "auto",
    }

    t0 = time.monotonic()
    try:
        resp = await client.post(url, files=files, data=data, timeout=600)
    except Exception as e:
        elapsed = time.monotonic() - t0
        return {"idx": idx, "ok": False, "elapsed": elapsed, "error": str(e)}

    elapsed = time.monotonic() - t0

    if resp.status_code != 200:
        return {"idx": idx, "ok": False, "elapsed": elapsed, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    return {"idx": idx, "ok": True, "elapsed": elapsed}


async def run_bench(pdf_dir: str, base_url: str, concurrency: int) -> dict:
    """Run the benchmark and return aggregated results."""
    pdfs = sorted(Path(pdf_dir).glob("*.pdf"))
    if not pdfs:
        print("No PDFs found in", pdf_dir, file=sys.stderr)
        sys.exit(1)

    print(f"  Benchmark: {len(pdfs)} PDFs, concurrency={concurrency}, target={base_url}", flush=True)

    sem = asyncio.Semaphore(concurrency)

    async def bounded(pdf: Path, idx: int) -> dict:
        async with sem:
            return await submit_one(client, base_url, pdf, idx)

    results: list[dict] = [None] * len(pdfs)

    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
        t_start = time.monotonic()

        async def worker(pdf: Path, idx: int) -> None:
            results[idx] = await bounded(pdf, idx)

        tasks = [asyncio.create_task(worker(pdf, i)) for i, pdf in enumerate(pdfs)]
        await asyncio.gather(*tasks)

        t_total = time.monotonic() - t_start

    # Aggregate
    ok_results = [r for r in results if r["ok"]]
    fail_results = [r for r in results if not r["ok"]]

    if fail_results:
        print(f"  FAILURES: {len(fail_results)}/{len(pdfs)}", file=sys.stderr)
        for r in fail_results:
            print(f"    PDF #{r['idx']}: {r.get('error', 'unknown')}", file=sys.stderr)

    elapsed_list = [r["elapsed"] for r in ok_results]
    if not elapsed_list:
        return {"error": "all requests failed", "concurrency": concurrency}

    elapsed_list.sort()
    n = len(elapsed_list)
    median = elapsed_list[n // 2] if n % 2 else (elapsed_list[n // 2 - 1] + elapsed_list[n // 2]) / 2
    req_per_sec = n / t_total if t_total > 0 else 0

    return {
        "concurrency": concurrency,
        "num_pdfs": len(pdfs),
        "num_ok": n,
        "total_time": round(t_total, 1),
        "min_time": round(elapsed_list[0], 1),
        "median_time": round(median, 1),
        "max_time": round(elapsed_list[-1], 1),
        "req_per_sec": round(req_per_sec, 3),
    }


def print_table_row(label: str, stats: dict, baseline_time: float | None = None) -> None:
    """Print one markdown table row."""
    if "error" in stats:
        print(f"| {label:<22} | {stats['concurrency']:<11} | FAILED |")
        return

    eff = ""
    if baseline_time is not None and stats["total_time"] > 0:
        # Efficiency = baseline_total / this_total * num_gpus_used
        # For baseline: num_gpus=1
        # For router configs: num_gpus=2
        # We'll compute efficiency in the aggregation step
        pass

    total = stats["total_time"]
    tmin = stats["min_time"]
    tmed = stats["median_time"]
    tmax = stats["max_time"]
    rps = stats["req_per_sec"]

    print(f"| {label:<22} | {stats['concurrency']:<11} | {total:<6.1f}s | {tmin:<5.1f}s | {tmed:<6.1f}s | {tmax:<5.1f}s | {rps:<6.3f} |")


def main():
    parser = argparse.ArgumentParser(description="GPU concurrency benchmark")
    parser.add_argument("pdf_dir", help="Directory containing PDF files")
    parser.add_argument("base_url", help="Server base URL")
    parser.add_argument("--concurrency", type=int, default=1, help="Max concurrent requests")
    parser.add_argument("--label", default="", help="Config label for output row")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of table row")
    args = parser.parse_args()

    stats = asyncio.run(run_bench(args.pdf_dir, args.base_url.rstrip("/"), args.concurrency))

    if args.json:
        print(json.dumps(stats))
    elif args.label:
        print_table_row(args.label, stats)
    else:
        print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
