# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A pair of small scripts that wrap [minerU](https://github.com/opendatalab/MinerU) for PDF → structured Markdown parsing on an NVIDIA CUDA host (currently 1× Tesla V100 / sm_70, torch+cu126). The wrapper hides CUDA env setup, GPU selection, batch staging, and minerU's noisy auxiliary output — one positional CLI that accepts any mix of PDF files and directories. Legacy AMD/ROCm notes live under `~/archive/mineru-rocm/` (GitHub archived: duguex/mineru-rocm) and historical `bench_results.md`.

## API Server

A minerU FastAPI server is available at a configurable LAN address.
- POST PDFs to `/file_parse` with `backend=pipeline`, `lang_list=["en"]`
- OpenAPI docs at `/docs`
- Client script: `python3 api_client.py paper.pdf http://<server>:<port>`
- `api_client.py -o` IS the parsed root (its `manifest.json` lands at `<out>/manifest.json`, and paper dirs at `<out>/<name>/`); the CLI wrapper's `-o` is the output root with `parsed/` inside. Same manifest schema either way.

For external access, forward a public port to this internal address.

## Common commands

There is no build, lint, or external test suite. The scripts are invoked directly.

```bash
# One PDF, two PDFs, a directory, or any combination
python3 mineru_wrapper.py paper.pdf
python3 mineru_wrapper.py pdf_dir/
python3 mineru_wrapper.py pdf_dir/ extra.pdf

# Re-parse PDFs that already have parsed/<name>/paper.md (the skip key)
python3 mineru_wrapper.py paper.pdf --force

# Custom output root (default: current directory)
python3 mineru_wrapper.py paper.pdf -o /tmp/out

# Multi-GPU when available (splits PDFs across GPUs in parallel)
python3 mineru_wrapper.py pdf_dir/ --gpus 0          # default on this host
```

Every run writes `output_dir/parsed/manifest.json`; when exactly one PDF is processed, the friendly path triple (`paper.md` / `images/` / `image-map.txt`) is also printed.

**Image-map script standalone** (rarely needed — wrapper calls this internally):
```bash
python3 map_mineru_images.py -m <paper.md> -o image-map.txt
```

**Logs** — every minerU invocation streams to `~/logs/mineru/run_<YYYYMMDD_HHMMSS>.log` (full stdout + stderr). The wrapper's `print()` lines appear in the terminal; the underlying minerU chatter is in the log file.

## Architecture

Three parse paths (CLI `mineru_wrapper.py`, router `batch_parse.py`, HTTP `api_client.py`) share two modules: `finalize.py` (post-processing: layout, image-map, orphan filter) and `bookkeeping.py` (identity: derived name, skip key, manifest schema). Both import `map_mineru_images.py` where relevant — image-map generation is in-process, no subprocess seam.

**`mineru_wrapper.py`** — entry point. Single `main()` function that flows:

1. **Discovery** (`collect_pdfs`) — resolve positional args to a `[(derived_name, abs_path)]` list. Mixes files and directories, deduplicates by derived name, warns on non-PDF inputs.
2. **Skip filter** — drop any PDF that already has `output_dir/parsed/<name>/paper.md`, unless `--force`. The same rule applies whether one or many PDFs are passed.
3. **Staging** — symlink each PDF into a `TemporaryDirectory` as `<derived_name>.pdf`. minerU honours its input filename when naming output dirs, so without this step a PDF like `Foo - 2020 - bar.pdf` would land in `Foo_-_2020_-_bar/auto/` while the wrapper looks under `Foo_2020_bar/auto/`.
4. **Env bootstrap** (`run_mineru`) — sources `~/mineru-cuda/mineru-cuda-env.sh`, pins `CUDA_VISIBLE_DEVICES` to the requested GPU, sets `MINERU_API_MAX_CONCURRENT_REQUESTS=1`, then shells out to `conda run -n mineru mineru -p <staged-tmpdir> -o <output> -b pipeline -m auto -l en`. Accepts a `gpu` parameter (default `"0"`). When `--gpus 0,1` is passed (multi-GPU machines only), papers are split evenly across GPUs (round-robin) and run in parallel via `ThreadPoolExecutor`. On failure, retries any PDFs whose `auto/` dir is missing.
5. **Post-processing** — for each paper: `finalize_output` (from `finalize.py`) relocates the raw tree, generates `image-map.txt` in-process, and drops orphan images.
6. **Manifest** — always written to `<output>/parsed/manifest.json` with per-paper `{name, pdf_path, paper_md, status}`.

**`finalize.py: finalize_output(name, src_tree, target_root)`** — the shared finalizer. Raw arrival (minerU's `src_tree/<name>/auto/`) moves `{<name>.md, images/}` to `target_root/<name>/{paper.md, images/}`, generates `image-map.txt` in-process via `build_image_map`, drops the auxiliary junk, and removes the raw wrapper when it differs from the target. Final arrival (batch/HTTP trees already at `target_root/<name>/`) only generates the map if missing and drops orphan images. Idempotent; image-map failure is non-fatal (degrades to no map — the orphan filter then keeps anything `paper.md` references).

The orphan filter deletes any image in `images/` that is referenced by neither `image-map.txt` nor `paper.md`; the decision lives in the pure `orphan_jpgs(images_dir, map_path, md_path)`. `paper.md` is the source of truth: real figures appear as `![](images/<hash>.jpg)`, equations as `$…$` LaTeX, tables as inline `<table>…</table>`. JPGs minerU extracted but paper.md doesn't reference are duplicates of one of the structured forms and are dropped. Same logic handles both layouts via arrival detection on `auto/`.

**`bookkeeping.py`** — the one owner of the glossary's identity and state concepts. `derive_name(pdf_path)` (filename → clean alphanumeric key) decides output dirs and the skip key everywhere; `paper_md_path(parsed_dir, name)` / `is_skipped(parsed_dir, name)` implement the skip key (`parsed/<name>/paper.md` exists → default skip, all three paths); `manifest_payload(settings, rows)` is the single manifest schema — rows use the glossary status vocabulary {parsed, failed, skipped} and the summary is computed, so the three writers cannot drift. Shell command strings use stdlib `shlex.quote`.

**`map_mineru_images.py`** — reads `paper.md` (minerU's structured Markdown), finds all `![](...)` image references, and extracts figure/table labels from the surrounding text. Produces `image-map.txt` with one line per image: `<hash>.jpg  →  FIG. 1(a)`.

Algorithm:
- Scans `paper.md` for `![](images/<hash>.jpg)` references in document order.
- For each, looks 400 chars ahead for `FIG. N` / `TABLE N` caption text. When both patterns match in the window, the **earliest** one wins — avoids confusing a "see Table I" mid-paragraph reference with the real Figure caption that opens it.
- Number group accepts arabic (`Fig. 1`), SI (`Fig. S11`), and chapter-style (`Fig. 1.1`); `TABLE` additionally accepts roman (`TABLE IV`). Each form is its own base, so `Fig. 1` and `Fig. S1` and `Fig. 1.1` do not merge.
- Images without a detectable caption inherit the previous figure's base label — they join the right group as `(b)`, `(c)`, … instead of being dumped into a separate bucket.
- After every ref has a base label, consecutive items sharing one are grouped: a run of ≥2 becomes `(a)`, `(b)`, `(c)` …; singletons keep the bare base label (no `(a)`). Groups ≥27 use double letters (`(aa)`, `(ab)`, … through `(zz)`); naïve `chr(ord('a')+i)` would overflow into control characters (i=36 produces `U+0085 NEXT LINE`, which Python's `splitlines()` treats as a line break and corrupts `image-map.txt`).
- Refs that appear before any caption at all (rare) fall back to `FIG. ??`.
- Images extracted by minerU but never embedded in `paper.md` are deleted from `images/` by the finalizer (`finalize.py`) — these are duplicates of content paper.md already expresses structurally (LaTeX formulas, markdown tables).

## Known limitations

### minerU / parser limitations

- **Equation-heavy papers yield empty `images/`.** Every extracted JPG was a formula rendering that `paper.md` already represents as LaTeX, so the orphan filter drops the whole set. Example: Grzybowski 2000 — 36 JPGs in → 0 retained, 0 entries in `image-map.txt`. Correct behaviour; if you need every PDF page as a raster regardless, use a different tool.
- **Caption-before-ref formats land in the `FIG. ??` bucket.** The mapper only looks 400 chars *after* each `![](...)` for a `FIG. N` / `TABLE N` caption. Review articles and book chapters that put the caption *before* the image (`Figure 2\n![](...)`), or that scatter panel labels (`A`, `B`, `C`) between adjacent refs without a `FIG. N` prefix, are not recognised; affected refs fall into the `FIG. ??` fallback bucket and get sub-labels `(a)`, `(b)`, …, `(aa)` … Example: Luo 2023 — 27 / 37 refs in `FIG. ??`. Downstream LaTeX templates can treat `FIG. ??` as "unidentified — skip, place in appendix, or pass to the vision model below".

### CUDA / deployment (this host)

- **1× Tesla V100-PCIE-32GB.** Prefer `deploy_api.sh` (single process). `deploy_router.sh` auto-detects GPU count via `nvidia-smi` (no longer hard-codes `0,1`).
- **VRAM contention:** ollama often occupies most of the 32GB. Free VRAM before batch runs, or keep `--worker-conc 1` / `MINERU_API_MAX_CONCURRENT_REQUESTS=1`.
- **Backend:** always `pipeline` for reliability. hybrid/vlm need extra VRAM and usually vLLM — not the default path on V100.
- **Stack:** conda env `mineru` = torch 2.12.1+cu126 + mineru 3.4.x. Do **not** install official cu130 wheels (no sm_70 kernels on V100). Env script unsets `HIP_VISIBLE_DEVICES` / `ROCM_HOME` so old ROCm settings do not leak in.

## Vision model

A local vision model is deployed (llama.cpp server, Qwen3.6, multimodal). Set `VISION_API_URL` environment variable to point to it.

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
r = subprocess.run(['curl','-s',f'{os.environ["VISION_API_URL"]}/v1/chat/completions',
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

## GPU Concurrency & mineru-router

### GPU isolation (CUDA)

On NVIDIA, `mineru-router` isolates workers with `CUDA_VISIBLE_DEVICES` alone — no HIP patch required.
(Historical ROCm hosts needed an extra `HIP_VISIBLE_DEVICES` set alongside CUDA; that path is unused here.)

### Starting the server

**Single V100 (recommended):**
```bash
nohup bash deploy_api.sh --host 127.0.0.1 --worker-conc 1 &
curl -s http://127.0.0.1:8001/health
```

**Multi-GPU router** (only when `nvidia-smi -L` shows >1 GPU):
```bash
nohup bash deploy_router.sh --host 127.0.0.1 --worker-conc 2 &
# or force: MINERU_ROUTER_LOCAL_GPUS=0,1 bash deploy_router.sh ...
curl -s http://127.0.0.1:8002/health | python3 -c "
import json, sys; h=json.load(sys.stdin)
for s in h['servers']: print(f'{s[\"server_id\"]} gpu={s[\"gpu\"]} healthy={s[\"healthy\"]}')
"
```

Router provides **dynamic least-loaded scheduling**: each request goes to the worker with
lowest `(queued + processing + pending) / max_concurrent_requests` ratio, randomized among ties.

### Server environment

```bash
source ~/mineru-cuda/mineru-cuda-env.sh
export CUDA_VISIBLE_DEVICES=0
export MINERU_API_MAX_CONCURRENT_REQUESTS=1
conda run -n mineru --no-capture-output \
  mineru-api --host 0.0.0.0 --port 8001
```

### Batch processing (large-scale, via router)

For large PDF corpora (hundreds+), use `batch_parse.py` which sends PDFs concurrently
to the router and provides checkpoint/resume:

```bash
# Start router first (see above), then:
python3 batch_parse.py \
  --src /path/to/pdf_dir \
  --output /mnt/shared/batch_out \
  --url http://127.0.0.1:8002 \
  --concurrency 4 \
  --max-retries 3

# Monitor progress in another terminal:
watch -n 10 python3 batch_status.py /mnt/shared/batch_out/parsed/
```

Features:
- Sends one PDF per `/file_parse` request, concurrent via asyncio Semaphore
- Saves `paper.md` + `images/` from API response, then finalizes in-process via `finalize.py` (image-map + orphan cleanup)
- Maintains `progress.json` checkpoint — Ctrl+C safe, re-run to resume; already-finalized papers (glossary skip key) are recorded as `skipped` and never re-queued
- Failed PDFs retried with exponential backoff (configurable `--max-retries`, `--retry-delay`)
- Writes `manifest.json` on completion
- `batch_status.py` shows progress bar, ETA, throughput, recent failures

For small batches or single PDFs, use `mineru_wrapper.py` directly — no router needed.

### Recommended configs

| Scenario | Tool | Command |
|----------|------|---------|
| Single PDF | `mineru_wrapper.py` | `mineru_wrapper.py paper.pdf` |
| Small batch (≤20) | `mineru_wrapper.py` | `mineru_wrapper.py dir/ --gpus 0,1` |
| Large batch (100+) | router + `batch_parse.py` | `deploy_router.sh` + `batch_parse.py` |
| Remote / API access | `api_client.py` | `python3 api_client.py paper.pdf http://<host>:<port>` |

### Benchmark results

See `bench_results.md`. Optimal config: router, 2 GPUs, concurrency=2 per worker
(3.75× throughput vs single-GPU sequential, zero failures).
`bench_concurrency.py` is the standalone benchmark tool.

## Testing

There is no automated test suite. Validate changes manually using these recipes — the `paper_example` corpus is the canonical fixture. Set `PAPER_EXAMPLE=<path>` and substitute in the commands below.

### 1. Unit test for `finalize.py` (no GPU, ~1 s)

The finalizer's edge cases (raw vs final arrival, idempotent re-runs, missing `auto/`, orphan-image filtering) are covered by a real test file:

```bash
python3 test_finalize.py
```

It promotes the old dry-run heredoc and adds final-mode (batch/HTTP arrival) cases plus the pure `orphan_jpgs` decision.

### 1b. Unit test for `bookkeeping.py` (no GPU, <1 s)

```bash
python3 test_bookkeeping.py
```

Covers `derive_name` transforms, the skip-key artifact check, and manifest summary computation.
```

### 2. End-to-end smoke test (~90 s for the smallest PDF)

The smallest PDF in `paper_example` is `Grzybowski_等_-_2000_-_Ewald_summation_...pdf` (85 KB, 7 pages). Its filename has both Chinese (`等`) and hyphens — perfect for catching `derive_name` ↔ minerU-output-stem mismatches.

```bash
rm -rf /tmp/mineru_smoke
PDF="$PAPER_EXAMPLE/Grzybowski_等_-_2000_-_Ewald_summation_of_electrostatic_interactions_in_molecular_dynamics_of_a_three-dimensional_system_wi.pdf"
python3 mineru_wrapper.py "$PDF" -o /tmp/mineru_smoke
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
cp "$PAPER_EXAMPLE"/Grzybowski_等_*.pdf /tmp/mineru_batch_in/
cp "$PAPER_EXAMPLE"/Batatia_等_*.pdf /tmp/mineru_batch_in/
rm -rf /tmp/mineru_batch_out
python3 mineru_wrapper.py /tmp/mineru_batch_in/ -o /tmp/mineru_batch_out
```

Expect: `Done: 2 parsed, 0 failed, 0 skipped`, two paper dirs under `parsed/`, one `manifest.json` listing both.

### When to run which

| Change | Tests to run |
|---|---|
| `finalize.py` body | Unit test (1), then smoke (2) |
| `main()` / CLI / skip | Smoke (2) + skip (3) |
| `collect_pdfs` / argparse | Skip (3) + batch (4) |
| `map_mineru_images.py` | Smoke (2) and inspect resulting `image-map.txt` |
| `run_mineru` / CUDA env | Smoke (2) — failure shows up in `~/logs/mineru/run_*.log` |

## Environment requirements (external to repo)

- conda env `mineru` with torch 2.12.1+cu126 + mineru[core] 3.4.x
- `~/mineru-cuda/mineru-cuda-env.sh` — CUDA env script (unsets HIP/ROCm)
- models already under `~/.cache/modelscope/...` (see `~/mineru.json`)
- `~/logs/mineru/` — created on first run; permissions must allow writes
- Optional legacy (archived/removed on this host): `~/archive/mineru-rocm/` + former env `torch_rocm72`

If the env or script is missing, the wrapper prints a warning but still attempts to run (so first-time setups get a chance to fail loudly from minerU itself rather than a silent wrapper error).

## Best practices for large batch runs (>1000 PDFs)

Lessons from the historical 13,076-PDF run (47 h, 100% success on 2× Vega 20 ROCm). On this **1× V100** host, expect lower concurrency and clear ollama VRAM first.

### 0. Pre-flight (read first)

- Read `CLAUDE.md` "Known limitations" and "GPU Concurrency" sections — past failures are documented.
- Free GPU memory: ollama often holds ~24GB; leave room for mineru pipeline models.
- Use `deploy_api.sh` on single GPU; `deploy_router.sh` only if multiple NVIDIA GPUs are present.

### 1. Small-batch test first

Before any 1000+ PDF run, do a 20-PDF test. Exposes integration issues fast:
- free VRAM / OOM under ollama coexistence
- `CUDA_VISIBLE_DEVICES` / single-worker health
- progress.json schema mismatches
- router task_id conflicts (if using router)
- backend left as hybrid (must force `pipeline`)
A 20-PDF test takes ~5 min and prevents 47-hour mistakes.

### 2. Service architecture

| Scale | Tool | Why |
|---|---|---|
| <20 PDFs | `mineru_wrapper.py` | No daemon, no router overhead |
| 20–200 (1 GPU) | `deploy_api.sh` + `batch_parse.py --concurrency 1` | Single V100 safe path |
| multi-GPU only | `deploy_router.sh` + `batch_parse.py` | Dynamic least-loaded scheduling |
| 1000+ | Same, with progress checkpoint | Resume on crash is mandatory |

Router's load formula: `score = (queued + processing + pending) / max_concurrent` — always picks the least-loaded healthy worker, randomized among ties. Far better than round-robin.

### 3. Background processes

**Always use `daemonize.py` (double-fork)**, not `nohup`/`setsid`/`disown`. Bash tool timeouts, SSH drops, and Ctrl+C all kill process groups; only double-fork detaches fully.

Wrapper pattern for any long-running job:
```bash
# run_batch.sh: pkill old, start API, wait health, daemonize worker
python3 daemonize.py bash deploy_api.sh --host 127.0.0.1 --worker-conc 1
# ... wait for /health on :8001 ...
python3 daemonize.py python3 batch_parse.py --src ... --url http://127.0.0.1:8001 --concurrency 1
```

### 4. CUDA / V100 gotchas

- **Do not install cu130 torch wheels** — V100 (sm_70) is dropped from those builds; use cu126.
- **Watch free VRAM** — with ollama running, keep concurrency at 1.
- **Unset HIP/ROCm env** — `mineru-cuda-env.sh` does this; avoid sourcing archived `~/archive/mineru-rocm/mineru-rocm-env.sh` on this host.
- **Never `exec` in manually-launched deploy scripts** — kills server on parent shell exit. Reserve `exec` for systemd (`deploy_api.sh` uses `exec` intentionally for systemd).

### 5. Per-PDF failure handling

- Submit PDFs individually (`/file_parse` per request), not in one batched call.
- Retry with exponential backoff: 15s, 30s, 45s. Most failures are transient (router restart, GPU OOM).
- Persist `progress.json` after every PDF (atomic write: tmp + rename). Crash-safe resume.
- Skip key: `parsed/<name>/paper.md` exists → skip. Independent of `progress.json` for cross-tool consistency.
- Have a `wrapper-solo` fallback path for PDFs that consistently fail with router HTTP 409 / task_id conflicts (router has a task_id collision bug for some papers).

### 6. Progress visibility

- Terminal stdout: one line per PDF `[N/total] name OK 12.3s` — easy to grep.
- `progress.json` for stats (works across restarts).
- `batch_status.py` for human-readable summary (progress bar, ETA, rate).
- Run `watch -n 10 batch_status.py` in another terminal — don't tail logs manually.

### 7. Cleanup

`mineru-api` workers load ~30 GB of model weights per GPU and never release them when idle. After every batch run:

```bash
pkill -9 -f "mineru-router"
pkill -9 -f "mineru.cli.fast_api"
pkill -9 -f "conda run.*mineru"
```

`rocm-smi` should show VRAM 0% on both GPUs afterward. Workers don't gracefully exit on their own.

### 8. Manifest schema

All three writers (`mineru_wrapper.py`, `batch_parse.py`, `api_client.py`) share one schema via `bookkeeping.manifest_payload`: rows use the glossary status vocabulary {parsed, failed, skipped}, and `summary: {total, parsed, failed, skipped}` is computed from the rows at write time. `batch_parse.py`'s internal `progress.json` keeps its own transient vocabulary (done / failed / pending / skipped) — that is the checkpoint's private state, read by `batch_status.py`, and never merged into manifests.

### 9. Checklist (copy-paste)

```
[ ] Read CLAUDE.md and bench_results.md
[ ] 20-PDF smoke test (router + batch_parse)
[ ] deploy_router.sh uses --local-gpus=0,1 (not auto)
[ ] deploy_router.sh has no `exec`
[ ] router + batch_parse both daemonized via daemonize.py
[ ] progress.json checkpointed after each PDF
[ ] batch_status.py running in another terminal
[ ] pkill all mineru processes when done
[ ] manifest.json normalized to {total, done, failed, pending}
```

### 10. Throughput expectations

Phys Rev corpus (~30 pages average, mixed text/figures):
- 1 GPU, 1 worker: ~250 PDFs/h
- 2 GPU, 2 workers × conc 2 (router): ~320 PDFs/h
- 2 GPU, 2 workers × conc 3: OOM, throughput drops

12K PDFs ≈ 38–47 hours on the recommended config. Run unattended over a weekend.

## Agent skills

### Issue tracker

Issues live as GitHub issues in `duguex/mineru_wrapper`, managed with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` at the repo root plus `docs/adr/`. See `docs/agents/domain.md`.
