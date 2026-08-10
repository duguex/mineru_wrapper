#!/usr/bin/env python3
"""minerU API client — send PDFs to a remote minerU FastAPI server.

Usage:
    python3 api_client.py paper.pdf http://<server>:<port>
    python3 api_client.py a.pdf b.pdf http://<server>:<port> -o /tmp/out
    python3 api_client.py dir/ http://<server>:<port>
    python3 api_client.py dir/ extra.pdf http://<server>:<port> --async
    python3 api_client.py dir/ http://<server>:<port> --force   # re-parse parsed/

The server should use the `pipeline` backend (recommended on V100; hybrid/vlm need extra VRAM/vLLM).
"""
import argparse
import json
import shutil
import sys
import time
import uuid
from pathlib import Path

import httpx

from bookkeeping import (
    derive_name,
    is_skipped,
    manifest_payload,
    paper_md_path,
    paper_row,
    paper_status,
)
from finalize import finalize_output


def main():
    parser = argparse.ArgumentParser(description="minerU API client")
    parser.add_argument("paths", nargs="+", help="PDF file(s) or director(ies) of PDFs")
    parser.add_argument("url", help="Server base URL (e.g. http://<server>:<port>)")
    parser.add_argument("-o", "--output", default="./parsed",
                        help="Output directory (default: ./parsed)")
    parser.add_argument("--force", action="store_true",
                        help="Re-parse already-processed PDFs")
    parser.add_argument("--async", dest="use_async", action="store_true",
                        help="Use async /tasks endpoint instead of sync /file_parse")
    parser.add_argument("--lang", default="en", help="OCR language (default: en)")
    parser.add_argument("--no-formula", action="store_true", help="Disable formula parsing")
    parser.add_argument("--no-table", action="store_true", help="Disable table parsing")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")

    # Collect PDFs from all paths. Dedupe twice: by resolved path and by derived
    # name — two files collapsing to one key would clobber each other.
    pdfs = []
    seen_paths = set()
    seen_names = set()

    def _add(path: Path):
        abspath = str(path.resolve())
        if abspath in seen_paths:
            return
        name = derive_name(str(path))
        if name in seen_names:
            print(f"Warning: {path.name} collides with another PDF's derived "
                  f"name ({name}), skipping", file=sys.stderr)
            return
        seen_paths.add(abspath)
        seen_names.add(name)
        pdfs.append(path)

    for p in args.paths:
        path = Path(p)
        if path.is_file() and path.suffix.lower() == ".pdf":
            _add(path)
        elif path.is_dir():
            for f in sorted(path.glob("*.pdf")) + sorted(path.glob("*.PDF")):
                _add(f)
        else:
            print(f"Warning: {p} is not a PDF or directory, skipping", file=sys.stderr)

    if not pdfs:
        print("No PDFs found", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Glossary skip key: an existing artifact is default-skipped unless --force.
    to_send = []
    rows = []
    for pdf in pdfs:
        name = derive_name(str(pdf))
        if not args.force and is_skipped(output_dir, name):
            print(f"  SKIP  {name}: {pdf}")
            rows.append(paper_row(
                name, str(pdf.resolve()),
                str(paper_md_path(output_dir, name)),
                paper_status(paper_md_path(output_dir, name), skipped=True)))
        else:
            to_send.append(pdf)

    if not to_send:
        print("All PDFs already parsed. Use --force to re-parse.")
        manifest = manifest_payload(_api_settings(base_url, args), rows)
        _write_manifest(output_dir, manifest)
        return

    print(f"Processing {len(to_send)} PDF(s) in one batch...", flush=True)

    if args.use_async:
        result = submit_async(base_url, to_send, args)
    else:
        result = submit_sync(base_url, to_send, args)

    if result is None:
        print("Request failed", file=sys.stderr)
        sys.exit(1)

    # Save results for each PDF, finalize to the canonical subtree
    # (in-process image-map + orphan cleanup), then write the manifest.
    results_dict = result.get("results", {})
    for pdf in to_send:
        name = derive_name(str(pdf))
        # Server result keys = uploaded filename stem (we upload pdf.name);
        # our output key = derived name. Bind the two explicitly here.
        file_results = results_dict.get(pdf.stem, {})
        if not file_results:
            print(f"  No result for {pdf.name}", file=sys.stderr)
            rows.append(paper_row(
                name, str(pdf.resolve()),
                str(paper_md_path(output_dir, name)),
                paper_status(paper_md_path(output_dir, name))))
            continue

        paper_dir = output_dir / name
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
            for img_name, b64data in images.items():
                data = base64.b64decode(b64data.split(",", 1)[-1])
                (img_dir / img_name).write_bytes(data)
            print(f"  {pdf.name}: {len(images)} images")

        finalize_output(name, output_dir, output_dir)
        if not (paper_dir / "image-map.txt").exists():
            print(f"  {pdf.name}: (no image-map)")
        paper_md = paper_dir / "paper.md"
        rows.append(paper_row(
            name, str(pdf.resolve()), str(paper_md), paper_status(paper_md)))

    manifest = manifest_payload(_api_settings(base_url, args), rows)
    _write_manifest(output_dir, manifest)
    summary = manifest["summary"]
    print(f"Done: {summary['parsed']} parsed, {summary['failed']} failed, "
          f"{summary['skipped']} skipped")


def _api_settings(base_url: str, args) -> dict:
    """Run settings recorded in the manifest."""
    return {
        "source": [str(p) for p in args.paths],
        "output_dir": str(Path(args.output)),
        "url": base_url,
        "use_async": args.use_async,
        "force": args.force,
    }


def _write_manifest(output_dir: Path, manifest: dict):
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest: {manifest_path}")


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
