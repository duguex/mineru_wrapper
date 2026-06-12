#!/usr/bin/env python3
"""minerU API client — send PDFs to a remote minerU FastAPI server.

Usage:
    python3 api_client.py paper.pdf http://<server>:<port>
    python3 api_client.py a.pdf b.pdf http://<server>:<port> -o /tmp/out
    python3 api_client.py dir/ http://<server>:<port>
    python3 api_client.py dir/ extra.pdf http://<server>:<port> --async

The server must use the `pipeline` backend (ROCm limitation, not hybrid).
"""
import argparse
import json
import shutil
import sys
import time
import uuid
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description="minerU API client")
    parser.add_argument("paths", nargs="+", help="PDF file(s) or director(ies) of PDFs")
    parser.add_argument("url", help="Server base URL (e.g. http://<server>:<port>)")
    parser.add_argument("-o", "--output", default="./parsed",
                        help="Output directory (default: ./parsed)")
    parser.add_argument("--async", dest="use_async", action="store_true",
                        help="Use async /tasks endpoint instead of sync /file_parse")
    parser.add_argument("--lang", default="en", help="OCR language (default: en)")
    parser.add_argument("--no-formula", action="store_true", help="Disable formula parsing")
    parser.add_argument("--no-table", action="store_true", help="Disable table parsing")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")

    # Collect PDFs from all paths
    pdfs = []
    seen = set()
    for p in args.paths:
        path = Path(p)
        if path.is_file() and path.suffix.lower() == ".pdf":
            abspath = str(path.resolve())
            if abspath not in seen:
                pdfs.append(path)
                seen.add(abspath)
        elif path.is_dir():
            for f in sorted(path.glob("*.pdf")) + sorted(path.glob("*.PDF")):
                abspath = str(f.resolve())
                if abspath not in seen:
                    pdfs.append(f)
                    seen.add(abspath)
        else:
            print(f"Warning: {p} is not a PDF or directory, skipping", file=sys.stderr)

    if not pdfs:
        print("No PDFs found", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Processing {len(pdfs)} PDF(s) in one batch...", flush=True)

    if args.use_async:
        result = submit_async(base_url, pdfs, args)
    else:
        result = submit_sync(base_url, pdfs, args)

    if result is None:
        print("Request failed", file=sys.stderr)
        sys.exit(1)

    # Save results for each PDF
    results_dict = result.get("results", {})
    for pdf in pdfs:
        file_results = results_dict.get(pdf.stem, {})
        if not file_results:
            print(f"  No result for {pdf.name}", file=sys.stderr)
            continue

        paper_dir = output_dir / pdf.stem
        paper_dir.mkdir(parents=True, exist_ok=True)

        md_content = file_results.get("md_content")
        if md_content:
            (paper_dir / "paper.md").write_text(md_content, encoding="utf-8")
            print(f"  {pdf.name}: paper.md ({len(md_content)} chars)")

        images = file_results.get("images", {})
        if images:
            import base64
            img_dir = paper_dir / "images"
            img_dir.mkdir(exist_ok=True)
            for name, b64data in images.items():
                data = base64.b64decode(b64data.split(",", 1)[-1])
                (img_dir / name).write_bytes(data)
            print(f"  {pdf.name}: {len(images)} images")


def submit_sync(base_url: str, pdfs: list[Path], args) -> dict | None:
    """Use synchronous /file_parse endpoint (batch all PDFs in one request)."""
    url = f"{base_url}/file_parse"
    files = []
    for pdf in pdfs:
        f = open(pdf, "rb")
        files.append(("files", (pdf.name, f, "application/pdf")))
    data = {
        "backend": "pipeline",
        "parse_method": "auto",
        "lang_list": [args.lang],
        "formula_enable": str(not args.no_formula).lower(),
        "table_enable": str(not args.no_table).lower(),
        "return_md": "true",
        "return_images": "true",
    }
    try:
        resp = httpx.post(url, files=files, data=data, timeout=600)
    except Exception as e:
        print(f"  Request failed: {e}", file=sys.stderr)
        return None
    finally:
        for _, (_, fh, _) in files:
            fh.close()

    if resp.status_code != 200:
        print(f"  Server error {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        return None

    return resp.json()


def submit_async(base_url: str, pdfs: list[Path], args) -> dict | None:
    """Use async /tasks endpoint (batch all PDFs in one request)."""
    submit_url = f"{base_url}/tasks"
    files = []
    for pdf in pdfs:
        f = open(pdf, "rb")
        files.append(("files", (pdf.name, f, "application/pdf")))
    data = {
        "backend": "pipeline",
        "parse_method": "auto",
        "lang_list": [args.lang],
        "formula_enable": str(not args.no_formula).lower(),
        "table_enable": str(not args.no_table).lower(),
        "return_md": "true",
        "return_images": "true",
    }
    try:
        resp = httpx.post(submit_url, files=files, data=data, timeout=120)
    except Exception as e:
        print(f"  Submit failed: {e}", file=sys.stderr)
        return None
    finally:
        for _, (_, fh, _) in files:
            fh.close()

    if resp.status_code != 202:
        print(f"  Submit error {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        return None

    payload = resp.json()
    task_id = payload["task_id"]
    status_url = payload["status_url"]
    result_url = payload["result_url"]

    # Poll until done
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            sr = httpx.get(status_url, timeout=30)
        except Exception as e:
            print(f"  Status poll failed: {e}", file=sys.stderr)
            time.sleep(2)
            continue
        if sr.status_code != 200:
            time.sleep(2)
            continue
        st = sr.json().get("status")
        print(f"  Status: {st}", flush=True)
        if st in ("completed", "failed"):
            break
        time.sleep(2)
    else:
        print("  Timed out waiting for task", file=sys.stderr)
        return None

    # Fetch result
    try:
        rr = httpx.get(result_url, timeout=120)
    except Exception as e:
        print(f"  Result fetch failed: {e}", file=sys.stderr)
        return None

    if rr.status_code == 200:
        return rr.json()
    print(f"  Result error {rr.status_code}", file=sys.stderr)
    return None


if __name__ == "__main__":
    main()
