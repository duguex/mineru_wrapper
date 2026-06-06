# minerU Wrapper

Single command for PDF parsing with ROCm GPU setup, image mapping, and output standardization.

## Requirements

- minerU conda env: `torch_rocm72`
- ROCm env script: `~/mineru-rocm/mineru-rocm-env.sh`
- GPU 0 typically occupied (llama-server); wrapper auto-selects GPU 1

## Usage

```bash
# Parse a single PDF
python3 /home/duguex/scripts/mineru_wrapper.py paper.pdf

# Parse all PDFs in a directory, or mix files + dirs
python3 /home/duguex/scripts/mineru_wrapper.py pdf_dir/
python3 /home/duguex/scripts/mineru_wrapper.py pdf_dir/ extra.pdf

# Re-parse PDFs that already produced parsed/<name>/paper.md
python3 /home/duguex/scripts/mineru_wrapper.py paper.pdf --force

# Custom output root
python3 /home/duguex/scripts/mineru_wrapper.py paper.pdf -o /tmp/out
```

Default output root is the current directory (`.`).

## Output Structure

```
output_dir/parsed/<name>/
    paper.md           structured Markdown with LaTeX formulas
    images/            extracted figures (JPG, hash filenames)
    image-map.txt      hash → figure label mapping
                       (e.g., a1b2c3d4.jpg → FIG. 1(a)  (page 3, 1-based))

output_dir/parsed/manifest.json   per-paper {name, pdf_path, paper_md, status}
```

`manifest.json` is written for every run (single PDF or many).

## Internals

The wrapper:
1. Resolves the positional args (`collect_pdfs`) into a deduplicated list of `(derived_name, abs_path)`
2. Skips any PDF that already has `parsed/<name>/paper.md` unless `--force`
3. Symlinks the remaining PDFs into a `TemporaryDirectory` under their derived names (so minerU's output directories match)
4. Sources `~/mineru-rocm/mineru-rocm-env.sh`, sets `HIP_VISIBLE_DEVICES=1` and `MINERU_API_MAX_CONCURRENT_REQUESTS=1`
5. Runs minerU once with `-b pipeline -m auto -l en` over the staged directory; on failure, retries each missing paper individually
6. Generates `image-map.txt` via `map_mineru_images.py` and standardizes each paper's output to `parsed/<name>/{paper.md, images/, image-map.txt}`
7. Writes `parsed/manifest.json`

## Logs

Every minerU invocation writes a full stdout+stderr log to `~/logs/mineru/run_<YYYYMMDD_HHMMSS>.log`. The wrapper's own `print()` lines go only to the terminal.

## Sanity check

Smoke-test against a small PDF (~90 s):

```bash
python3 /home/duguex/scripts/mineru_wrapper.py /home/duguex/paper_example/LS20649.pdf -o /tmp/out
```

Expect `paper.md`, `images/`, `image-map.txt` under `/tmp/out/parsed/LS20649/`, plus a manifest listing one paper with `"status": "parsed"`. See `CLAUDE.md` for the full test catalog.
