# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A pair of small scripts that wrap [minerU](https://github.com/opendatalab/MinerU) for PDF → structured Markdown parsing on a ROCm GPU box. The wrapper hides ROCm env setup, GPU selection, batch staging, and minerU's noisy auxiliary output — one positional CLI that accepts any mix of PDF files and directories.

## Common commands

There is no build, lint, or external test suite. The scripts are invoked directly.

```bash
# One PDF, two PDFs, a directory, or any combination
python3 /home/duguex/scripts/mineru_wrapper.py paper.pdf
python3 /home/duguex/scripts/mineru_wrapper.py pdf_dir/
python3 /home/duguex/scripts/mineru_wrapper.py pdf_dir/ extra.pdf

# Re-parse PDFs that already have parsed/<name>/paper.md (the skip key)
python3 /home/duguex/scripts/mineru_wrapper.py paper.pdf --force

# Custom output root (default: current directory)
python3 /home/duguex/scripts/mineru_wrapper.py paper.pdf -o /tmp/out
```

Every run writes `output_dir/parsed/manifest.json`; when exactly one PDF is processed, the friendly path triple (`paper.md` / `images/` / `image-map.txt`) is also printed.

**Image-map script standalone** (rarely needed — wrapper calls this internally):
```bash
python3 /home/duguex/scripts/map_mineru_images.py -i <content_list_v2.json> -o image-map.txt
```

**Logs** — every minerU invocation streams to `~/logs/mineru/run_<YYYYMMDD_HHMMSS>.log` (full stdout + stderr). The wrapper's `print()` lines appear in the terminal; the underlying minerU chatter is in the log file.

## Architecture

Two scripts with a one-way dependency: `mineru_wrapper.py` calls `map_mineru_images.py` as a subprocess.

**`mineru_wrapper.py`** — entry point. Single `main()` function that flows:

1. **Discovery** (`collect_pdfs`) — resolve positional args to a `[(derived_name, abs_path)]` list. Mixes files and directories, deduplicates by derived name, warns on non-PDF inputs.
2. **Skip filter** — drop any PDF that already has `output_dir/parsed/<name>/paper.md`, unless `--force`. The same rule applies whether one or many PDFs are passed.
3. **Staging** — symlink each PDF into a `TemporaryDirectory` as `<derived_name>.pdf`. minerU honours its input filename when naming output dirs, so without this step a PDF like `Foo - 2020 - bar.pdf` would land in `Foo_-_2020_-_bar/auto/` while the wrapper looks under `Foo_2020_bar/auto/`.
4. **Env bootstrap** (`run_mineru`) — sources `~/mineru-rocm/mineru-rocm-env.sh`, pins `HIP_VISIBLE_DEVICES=1` (GPU 0 is occupied by llama-server), sets `MINERU_API_MAX_CONCURRENT_REQUESTS=1`, then shells out to `conda run -n torch_rocm72 mineru -p <staged-tmpdir> -o <output> -b pipeline -m auto -l en`. On failure, retries any PDFs whose `auto/` dir is missing.
5. **Post-processing** — for each paper: `generate_image_map` (subprocess to `map_mineru_images.py`) then `standardize_output`.
6. **Manifest** — always written to `<output>/parsed/manifest.json` with per-paper `{name, pdf_path, paper_md, status}`.

**`standardize_output(name, raw_parent, target_dir)`** moves minerU's `raw_parent/<name>/auto/{<name>.md, images/, image-map.txt, junk}` to `target_dir/<name>/{paper.md, images/, image-map.txt}`, `rmtree`s `auto/` whole (everything still in there is minerU auxiliary output), and removes the raw wrapper dir only when it differs from `paper_dir`. Same logic handles both the single-PDF layout (raw_parent ≠ target_dir) and batch layout (raw_parent == target_dir).

`derive_name` (filename → clean alphanumeric key) is the one small utility worth knowing; shell command strings use stdlib `shlex.quote`.

**`map_mineru_images.py`** — pure function `build_image_map(path) -> (text, groups)`. Consumes minerU's `content_list_v2.json` and produces `image-map.txt` (one line per image: `<hash>.jpg  →  FIG. 1(a)  (page 3, 1-based)`). The grouping heuristic:
- Full-width items (bbox x2-x0 > 60% of page max x) are standalone (typically tables).
- On a page with exactly **one** captioned figure + uncaptioned items → all items become subfigures `(a)`, `(b)`, … in reading order.
- Multiple captioned figures → each is standalone.
- Items are partitioned into rows (bbox y within `ROW_TOLERANCE=30`) and within a row sorted by bbox x.

The figure-label regex accepts `FIG. 1` / `Figure 1a` / `TABLE IV` / `Table 3` (case-insensitive). Coverage is incomplete in practice — many minerU `content_list_v2.json` entries surface as `equation` or `text` blocks and never enter the mapping, and the regex does not catch `Fig\b` (no period) or `Figure 1A` (uppercase subscript). Improving recall is a known follow-up.

## Output structure (canonical, post-wrapper)

```
output_dir/parsed/<name>/
    paper.md           structured Markdown with LaTeX formulas
    images/            extracted figures (JPG, hash filenames)
    image-map.txt      hash → figure label mapping

output_dir/parsed/manifest.json   (always written, one entry per processed paper)
```

In LaTeX/Beamer, point `\graphicspath{{.../parsed/<name>/images/}}` and reference images by their hash filename (`a1b2c3d4.jpg`); the mapping from hash → caption label is what `image-map.txt` records.

## Testing

There is no automated test suite. Validate changes manually using these recipes — the `paper_example` corpus (`/home/duguex/paper_example/`) is the canonical fixture.

### 1. Dry-run unit test for `standardize_output` (no GPU, ~1 s)

The output-move logic has fine-grained edge cases (single vs batch layout, idempotent re-runs, missing `auto/`). This harness exercises them against a mocked minerU output tree:

```bash
python3 - <<'PY'
import sys, shutil
from pathlib import Path
sys.path.insert(0, "/home/duguex/scripts")
from mineru_wrapper import standardize_output

def make(parent, name):
    """Mock the minerU auto/ layout."""
    auto = parent / name / "auto"
    auto.mkdir(parents=True)
    (auto / f"{name}.md").write_text("# Mock\n")
    (auto / "image-map.txt").write_text("abc.jpg → FIG. 1 (page 1)\n")
    for suffix in ("_layout.pdf", "_origin.pdf", "_span.pdf",
                   "_middle.json", "_model.json", "_content_list_v2.json"):
        (auto / f"{name}{suffix}").write_bytes(b"junk")
    (auto / "images").mkdir()
    (auto / "images" / "abc.jpg").write_bytes(b"jpg")

import tempfile
with tempfile.TemporaryDirectory() as td:
    td = Path(td)

    # Single mode: raw_parent ≠ target_dir → raw wrapper is removed
    make(td / "single", "demo")
    r = standardize_output("demo", td / "single", td / "single" / "parsed")
    assert r.exists() and r.name == "paper.md"
    assert (td / "single" / "parsed" / "demo" / "images" / "abc.jpg").exists()
    assert not (td / "single" / "demo").exists(), "raw wrapper should be gone"

    # Batch mode: raw_parent == target_dir → wrapper survives, auto/ doesn't
    make(td / "batch", "demo")
    r = standardize_output("demo", td / "batch", td / "batch")
    assert r.exists()
    assert (td / "batch" / "demo" / "images" / "abc.jpg").exists()
    assert not (td / "batch" / "demo" / "auto").exists()

    # Idempotent re-run: another minerU output landing on top should work
    make(td / "batch", "demo")
    standardize_output("demo", td / "batch", td / "batch")
    assert (td / "batch" / "demo" / "paper.md").exists()

    # Missing auto/ → None, no crash
    (td / "missing" / "ghost").mkdir(parents=True)
    assert standardize_output("ghost", td / "missing", td / "missing" / "parsed") is None

print("dry-run OK")
PY
```

### 2. End-to-end smoke test (~90 s for the smallest PDF)

The smallest PDF in `paper_example` is `Grzybowski_等_-_2000_-_Ewald_summation_...pdf` (85 KB, 7 pages). Its filename has both Chinese (`等`) and hyphens — perfect for catching `derive_name` ↔ minerU-output-stem mismatches.

```bash
rm -rf /tmp/mineru_smoke
PDF="/home/duguex/paper_example/Grzybowski_等_-_2000_-_Ewald_summation_of_electrostatic_interactions_in_molecular_dynamics_of_a_three-dimensional_system_wi.pdf"
python3 /home/duguex/scripts/mineru_wrapper.py "$PDF" -o /tmp/mineru_smoke
```

Expect ~90 s, exit 0, and exactly this final layout:
```
/tmp/mineru_smoke/parsed/manifest.json
/tmp/mineru_smoke/parsed/Grzybowski_等_2000_..._wi/{paper.md, image-map.txt, images/}
```
No stray `auto/` directories anywhere. `manifest.json` must have `"status": "parsed"`.

### 3. Skip / idempotency check (~1 s, no minerU)

Immediately re-run the smoke test above. Expect:
```
  SKIP  Grzybowski_等_2000_..._wi: <path>
All PDFs already parsed. Use --force to re-parse.
```
This proves the skip key (`parsed/<name>/paper.md`) is the same in both write and read paths. minerU should not be invoked — the whole run takes under a second.

### 4. Batch (N≥2) smoke test (~100 s for two small PDFs)

```bash
mkdir -p /tmp/mineru_batch_in
cp /home/duguex/paper_example/Grzybowski_等_*.pdf /tmp/mineru_batch_in/
cp /home/duguex/paper_example/Batatia_等_*.pdf /tmp/mineru_batch_in/
rm -rf /tmp/mineru_batch_out
python3 /home/duguex/scripts/mineru_wrapper.py /tmp/mineru_batch_in/ -o /tmp/mineru_batch_out
```

Expect: `Done: 2 parsed, 0 failed, 0 skipped`, two paper dirs under `parsed/`, one `manifest.json` listing both.

### When to run which

| Change | Tests to run |
|---|---|
| `standardize_output` body | Dry-run (1), then smoke (2) |
| `main()` / CLI / skip | Smoke (2) + skip (3) |
| `collect_pdfs` / argparse | Skip (3) + batch (4) |
| `map_mineru_images.py` | Smoke (2) and inspect resulting `image-map.txt` |
| `run_mineru` / ROCm env | Smoke (2) — failure shows up in `~/logs/mineru/run_*.log` |

## Environment requirements (external to repo)

- conda env `torch_rocm72` with minerU installed
- `~/mineru-rocm/mineru-rocm-env.sh` — ROCm env script
- `~/logs/mineru/` — created on first run; permissions must allow writes

If the env or script is missing, the wrapper prints a warning but still attempts to run (so first-time setups get a chance to fail loudly from minerU itself rather than a silent wrapper error).
