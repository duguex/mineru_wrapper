# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A pair of small scripts that wrap [minerU](https://github.com/opendatalab/MinerU) for PDF → structured Markdown parsing on a ROCm GPU box. The wrapper hides ROCm env setup, GPU selection, batch orchestration, and minerU's noisy auxiliary output, exposing one clean command per mode.

## Common commands

There is no build, lint, or test suite. The scripts are invoked directly.

**Single PDF** — parses one file, standardizes output under `parsed/<name>/`:
```bash
python3 /home/duguex/scripts/mineru_wrapper.py --single paper.pdf [output_dir]
```

**Batch directory** — parses all PDFs, writes `parsed/manifest.json`:
```bash
python3 /home/duguex/scripts/mineru_wrapper.py --batch pdf_dir/ [--force]
```
Use `--force` to re-parse papers that already have `output_dir/slides/<name>/slides.tex` (the batch skip key).

**Image-map script standalone** (rarely needed — wrapper calls this internally):
```bash
python3 /home/duguex/scripts/map_mineru_images.py -i <content_list_v2.json> -o image-map.txt
```

**Logs** — every minerU run streams to `~/logs/mineru/run_<YYYYMMDD_HHMMSS>.log` (full stdout + stderr). The wrapper's `print()` lines appear in the terminal; the underlying minerU chatter is in the log file.

## Architecture

Two scripts with a clear one-way dependency: `mineru_wrapper.py` calls `map_mineru_images.py` as a subprocess.

**`mineru_wrapper.py`** — entry point. Three concerns:
1. **Env bootstrap** (`run_mineru`, `mineru_available`) — sources `~/mineru-rocm/mineru-rocm-env.sh`, pins `HIP_VISIBLE_DEVICES=1` (GPU 0 is occupied by llama-server), sets `MINERU_API_MAX_CONCURRENT_REQUESTS=1`, then shells out to `conda run -n torch_rocm72 mineru -p <input> -o <output> -b pipeline -m auto -l en`.
2. **Output standardization** (`standardize_output`) — minerU emits `<name>/auto/{*.md, *.pdf, *.json, images/}`. The wrapper renames `<name>.md` → `paper.md`, moves `images/` and `image-map.txt` up one level, then deletes the auxiliary `_layout.pdf` / `_middle.json` / `_model.json` / `_origin.pdf` / `_span.pdf` files and the empty `auto/` / `<name>/` parents.
3. **Modes** — `parse_single` (one PDF, raw output → `parsed/<name>/`) and `parse_batch` (staging dir of symlinks under a `tempfile.mkdtemp`, minerU native batch mode, retry-the-failed loop on partial failure, writes `manifest.json`).

`derive_name` (filename → clean alphanumeric key) and `shlex_quote` (single-quote escaping for the `bash -c` command string) are the two small utilities worth knowing.

**`map_mineru_images.py`** — pure function `build_image_map(path) -> (text, groups)`. Consumes minerU's `content_list_v2.json` and produces `image-map.txt` (one line per image: `<hash>.jpg  →  FIG. 1(a)  (page 3)`). The grouping heuristic:
- Full-width items (bbox x2-x0 > 60% of page max x) are standalone (typically tables).
- On a page with exactly **one** captioned figure + uncaptioned items → all items become subfigures `(a)`, `(b)`, … in reading order.
- Multiple captioned figures → each is standalone.
- Items are partitioned into rows (bbox y within `ROW_TOLERANCE=30`) and within a row sorted by bbox x.

The figure-label regex accepts `FIG. 1` / `Figure 1a` / `TABLE IV` / `Table 3` (case-insensitive).

## Output structure (canonical, post-wrapper)

```
output_dir/parsed/<name>/
    paper.md           structured Markdown with LaTeX formulas
    images/            extracted figures (JPG, hash filenames)
    image-map.txt      hash → figure label mapping

output_dir/parsed/manifest.json   (batch mode only)
```

In LaTeX/Beamer, point `\graphicspath{{.../parsed/<name>/images/}}` and reference images by their hash filename (`a1b2c3d4.jpg`); the mapping from hash → caption label is what `image-map.txt` records.

## Environment requirements (external to repo)

- conda env `torch_rocm72` with minerU installed
- `~/mineru-rocm/mineru-rocm-env.sh` — ROCm env script
- `~/logs/mineru/` — created on first run; permissions must allow writes

If the env or script is missing, the wrapper prints a warning but still attempts to run (so first-time setups get a chance to fail loudly from minerU itself rather than a silent wrapper error).
