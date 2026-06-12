#!/usr/bin/env python3
"""minerU API client — send PDFs to a remote minerU FastAPI server.

Usage:
    python3 api_client.py paper.pdf http://<server>:<port>
    python3 api_client.py paper.pdf http://<server>:<port> -o /tmp/out
    python3 api_client.py dir/ http://<server>:<port> --async

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
    parser.add_argument("path", help="PDF file or directory of PDFs")
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
    path = Path(args.path)

    # Collect PDFs
    pdfs = []
    if path.is_file() and path.suffix.lower() == ".pdf":
        pdfs = [path]
    elif path.is_dir():
        pdfs = sorted(path.glob("*.pdf")) + sorted(path.glob("*.PDF"))
    else:
        print(f"Error: {path} is not a PDF or directory", file=sys.stderr)
        sys.exit(1)

    if not pdfs:
        print("No PDFs found", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    for pdf in pdfs:
        print(f"\nProcessing: {pdf.name}", flush=True)

        if args.use_async:
            result = submit_async(base_url, pdf, args)
        else:
            result = submit_sync(base_url, pdf, args)

        if result is None:
            print(f"  FAILED: {pdf.name}", file=sys.stderr)
            continue

        # Save results (nested under results[pdf_stem])
        paper_dir = output_dir / pdf.stem
        paper_dir.mkdir(parents=True, exist_ok=True)

        file_results = result.get("results", {}).get(pdf.stem, {})

        md_content = file_results.get("md_content")
        if md_content:
            (paper_dir / "paper.md").write_text(md_content, encoding="utf-8")
            print(f"  paper.md: {len(md_content)} chars")

        images = file_results.get("images", {})
        if images:
            import base64
            img_dir = paper_dir / "images"
            img_dir.mkdir(exist_ok=True)
            for name, b64data in images.items():
                data = base64.b64decode(b64data.split(",", 1)[-1])
                (img_dir / name).write_bytes(data)
            print(f"  images:   {len(images)} files")

        print(f"  OK: {paper_dir / 'paper.md'}")


def submit_sync(base_url: str, pdf: Path, args) -> dict | None:
    """Use synchronous /file_parse endpoint."""
    url = f"{base_url}/file_parse"
    with open(pdf, "rb") as f:
        files = {"files": (pdf.name, f, "application/pdf")}
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

    if resp.status_code != 200:
        print(f"  Server error {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        return None

    return resp.json()


def submit_async(base_url: str, pdf: Path, args) -> dict | None:
    """Use async /tasks endpoint (submit, poll, fetch result)."""
    submit_url = f"{base_url}/tasks"
    with open(pdf, "rb") as f:
        files = {"files": (pdf.name, f, "application/pdf")}
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
