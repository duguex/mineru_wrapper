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
python3 /home/duguex/scripts/map_mineru_images.py -m <paper.md> -o image-map.txt
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

**`standardize_output(name, raw_parent, target_dir)`** moves minerU's `raw_parent/<name>/auto/{<name>.md, images/, image-map.txt, junk}` to `target_dir/<name>/{paper.md, images/, image-map.txt}`, then deletes any image in `images/` that is referenced by neither `image-map.txt` nor `paper.md`. `paper.md` is the source of truth: real figures appear as `![](images/<hash>.jpg)`, equations as `$…$` LaTeX, tables as inline `<table>…</table>`. JPGs minerU extracted but paper.md doesn't reference are duplicates of one of the structured forms and are dropped. Finally `rmtree`s `auto/` whole and removes the raw wrapper dir only when it differs from `paper_dir`. Same logic handles both single-PDF layout (raw_parent ≠ target_dir) and batch layout (raw_parent == target_dir).

`derive_name` (filename → clean alphanumeric key) is the one small utility worth knowing; shell command strings use stdlib `shlex.quote`.

**`map_mineru_images.py`** — reads `paper.md` (minerU's structured Markdown), finds all `![](...)` image references, and extracts figure/table labels from the surrounding text. Produces `image-map.txt` with one line per image: `<hash>.jpg  →  FIG. 1(a)`.

Algorithm:
- Scans `paper.md` for `![](images/<hash>.jpg)` references in document order.
- For each, looks 400 chars ahead for `FIG. N` / `TABLE N` caption text. When both patterns match in the window, the **earliest** one wins — avoids confusing a "see Table I" mid-paragraph reference with the real Figure caption that opens it.
- Number group accepts arabic (`Fig. 1`), SI (`Fig. S11`), and chapter-style (`Fig. 1.1`); `TABLE` additionally accepts roman (`TABLE IV`). Each form is its own base, so `Fig. 1` and `Fig. S1` and `Fig. 1.1` do not merge.
- Images without a detectable caption inherit the previous figure's base label — they join the right group as `(b)`, `(c)`, … instead of being dumped into a separate bucket.
- After every ref has a base label, consecutive items sharing one are grouped: a run of ≥2 becomes `(a)`, `(b)`, `(c)` …; singletons keep the bare base label (no `(a)`). Groups ≥27 use double letters (`(aa)`, `(ab)`, … through `(zz)`); naïve `chr(ord('a')+i)` would overflow into control characters (i=36 produces `U+0085 NEXT LINE`, which Python's `splitlines()` treats as a line break and corrupts `image-map.txt`).
- Refs that appear before any caption at all (rare) fall back to `FIG. ??`.
- Images extracted by minerU but never embedded in `paper.md` are deleted from `images/` by `standardize_output` — these are duplicates of content paper.md already expresses structurally (LaTeX formulas, markdown tables).

**Known limitation:** Coverage equals what minerU embeds in `paper.md`. Equation-heavy papers (e.g. Grzybowski 2000) end up with 0 entries in `image-map.txt` and an empty `images/` because every extracted JPG was a formula rendering already present as LaTeX. Verified on `paper_example`: LS20649 9→7, Batatia 29→7, Grzybowski 36→0; no real figure dropped.

## Vision model

A local vision model is deployed at `192.168.1.130:8001` (llama.cpp server, Qwen3.6, multimodal). It is configured in `~/.omp/agent/config.yml` as `vision` and `designer` roles.

**Purpose:** Spot-check ambiguous labels and resolve the rare `FIG. ??` case (refs that appear before any caption in paper.md). The vision model can look at an image and determine whether it's a FIGURE, TABLE, FORMULA, or SUBFIGURE.

**Usage via curl** (for batch inspection):
```bash
python3 -c "
import json, subprocess, base64, tempfile, os
b64 = base64.b64encode(open('image.jpg','rb').read()).decode()
payload = {'model':'unsloth/Qwen3.6','messages':[{'role':'user','content':[
    {'type':'image_url','image_url':{'url':f'data:image/jpeg;base64,{b64}'}},
    {'type':'text','text':'One word: FIGURE, TABLE, FORMULA, or SUBFIGURE?'}
]}],'max_tokens':512,'stream':False}
with tempfile.NamedTemporaryFile(mode='w',suffix='.json',delete=False) as f:
    json.dump(payload,f); tmp=f.name
r = subprocess.run(['curl','-s','http://192.168.1.130:8001/v1/chat/completions',
    '-H','Content-Type: application/json','-d',f'@{tmp}'],
    capture_output=True,text=True,timeout=60)
os.unlink(tmp)
print(json.loads(r.stdout)['choices'][0]['message'].get('content',''))
"
```
Each call takes 3-7 seconds. The model is a reasoning model; the answer appears in the `content` field (requires `max_tokens ≥ 512` — lower values truncate before the answer is emitted).

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

The output-move logic has fine-grained edge cases (single vs batch layout, idempotent re-runs, missing `auto/`, orphan-image filtering). This harness exercises them against a mocked minerU output tree:

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
    (auto / f"{name}.md").write_text("# Mock\n\n![](images/xyz.jpg)\n")
    (auto / "image-map.txt").write_text("abc.jpg  →  FIG. 1\n")
    for suffix in ("_layout.pdf", "_origin.pdf", "_span.pdf",
                   "_middle.json", "_model.json", "_content_list_v2.json"):
        (auto / f"{name}{suffix}").write_bytes(b"junk")
    (auto / "images").mkdir()
    (auto / "images" / "abc.jpg").write_bytes(b"jpg")     # in image-map → kept
    (auto / "images" / "xyz.jpg").write_bytes(b"jpg")     # in paper.md → kept
    (auto / "images" / "orphan.jpg").write_bytes(b"jpg")  # in neither → dropped

import tempfile
with tempfile.TemporaryDirectory() as td:
    td = Path(td)

    # Single mode: raw_parent ≠ target_dir → raw wrapper is removed
    make(td / "single", "demo")
    r = standardize_output("demo", td / "single", td / "single" / "parsed")
    imgs = td / "single" / "parsed" / "demo" / "images"
    assert r.exists() and r.name == "paper.md"
    assert (imgs / "abc.jpg").exists(), "image-map'd jpg should survive"
    assert (imgs / "xyz.jpg").exists(), "paper.md-referenced jpg should survive"
    assert not (imgs / "orphan.jpg").exists(), "orphan jpg should be filtered"
    assert not (td / "single" / "demo").exists(), "raw wrapper should be gone"

    # Batch mode: raw_parent == target_dir → wrapper survives, auto/ doesn't
    make(td / "batch", "demo")
    r = standardize_output("demo", td / "batch", td / "batch")
    imgs = td / "batch" / "demo" / "images"
    assert r.exists()
    assert (imgs / "abc.jpg").exists() and (imgs / "xyz.jpg").exists()
    assert not (imgs / "orphan.jpg").exists()
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
