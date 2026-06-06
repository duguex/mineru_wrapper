# minerU Wrapper

Single command for PDF parsing with ROCm GPU setup, image mapping, and output standardization.

## Requirements

- minerU conda env: `torch_rocm72`
- ROCm env script: `~/mineru-rocm/mineru-rocm-env.sh`
- GPU 0 typically occupied (llama-server), wrapper auto-selects GPU 1

## Usage

```bash
# Parse a single PDF
python3 /home/duguex/scripts/mineru_wrapper.py paper.pdf [output_dir]

# Parse all PDFs in a directory (or mix files + dirs)
python3 /home/duguex/scripts/mineru_wrapper.py pdf_dir/
python3 /home/duguex/scripts/mineru_wrapper.py pdf_dir/ extra.pdf

# Re-parse already-processed PDFs
python3 /home/duguex/scripts/mineru_wrapper.py paper.pdf --force
```

Default output root is the current directory (`.`).

## Output Structure

```
output_dir/parsed/<name>/
    paper.md           structured Markdown with LaTeX formulas
    images/            extracted figures (JPG, hash filenames)
    image-map.txt      hash → figure label mapping
                       (e.g., a1b2c3d4.jpg → FIG. 1(a) page 3)
```

For batch mode, `output_dir/parsed/manifest.json` lists all papers with status.

## Internals

The wrapper:
1. Sources `~/mineru-rocm/mineru-rocm-env.sh` for ROCm env
2. Sets `HIP_VISIBLE_DEVICES=1`, `MINERU_API_MAX_CONCURRENT_REQUESTS=1`
3. Runs minerU with `-b pipeline -m auto -l en` flags
4. Generates image-map.txt via `map_mineru_images.py`
5. Removes minerU auxiliary files (_layout.pdf, _middle.json, _model.json, _origin.pdf, _span.pdf)
6. Standardizes output to `parsed/<name>/{paper.md, images/, image-map.txt}`
