#!/usr/bin/env python3
"""GPU concurrency benchmark for mineru-api vs mineru-router.

Usage:
    python3 bench_concurrency.py <pdf_dir> <base_url> [--concurrency N]

Outputs a markdown row suitable for the comparison table.
"""
import argparse
import asyncio
import json
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
        await asyncio.gather(*tasks, return_exceptions=True)

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



def main():
    parser = argparse.ArgumentParser(description="GPU concurrency benchmark")
    parser.add_argument("pdf_dir", help="Directory containing PDF files")
    parser.add_argument("base_url", help="Server base URL")
    parser.add_argument("--concurrency", type=int, default=1, help="Max concurrent requests")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    stats = asyncio.run(run_bench(args.pdf_dir, args.base_url.rstrip("/"), args.concurrency))
    if args.json:
        print(json.dumps(stats))
    else:
        print(json.dumps(stats, indent=2))
