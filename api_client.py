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
import asyncio
import json
import sys
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
from parse_client import (
    ParseClientError,
    fetch_result_async,
    file_parse_sync,
    materialize_results,
    parse_params,
    poll_task_async,
    submit_task_async,
)


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

    params = parse_params(lang=args.lang, formula=not args.no_formula,
                          table=not args.no_table)
    try:
        if args.use_async:
            result = asyncio.run(_async_parse(base_url, to_send, params))
        else:
            result = file_parse_sync(base_url, to_send, params)
    except ParseClientError as e:
        print(f"Request failed: {e}", file=sys.stderr)
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
        saved = materialize_results(paper_dir, file_results)
        if saved["md_written"]:
            print(f"  {pdf.name}: paper.md ({saved['md_chars']} chars)")
        if saved["images_count"]:
            print(f"  {pdf.name}: {saved['images_count']} images")

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


async def _async_parse(base_url: str, pdfs: list[Path], params: dict) -> dict:
    """Full /tasks flow — submit, poll to a terminal status, fetch the result.

    A terminal 'failed' status still fetches the result payload so per-file
    failures land in the manifest (matches the pre-parse_client behavior);
    only a poll deadline raises.
    """
    async with httpx.AsyncClient() as client:
        status_url, result_url = await submit_task_async(
            client, base_url, pdfs, params)
        await poll_task_async(client, status_url)
        return await fetch_result_async(client, result_url)


if __name__ == "__main__":
    main()
