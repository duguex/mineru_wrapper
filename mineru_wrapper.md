# minerU Wrapper

Single command for PDF parsing with ROCm GPU setup, image mapping, and output standardization.
Also includes a FastAPI server deployment for remote PDF parsing.

## Requirements

- minerU conda env: `torch_rocm72`
- ROCm env script: `~/mineru-rocm/mineru-rocm-env.sh`

## Local CLI Usage

```bash
# Parse a single PDF
python3 ~/mineru_wrapper/mineru_wrapper.py paper.pdf

# Parse all PDFs in a directory, or mix files + dirs
python3 ~/mineru_wrapper/mineru_wrapper.py pdf_dir/
python3 ~/mineru_wrapper/mineru_wrapper.py pdf_dir/ extra.pdf

# Re-parse PDFs that already produced parsed/<name>/paper.md
python3 ~/mineru_wrapper/mineru_wrapper.py paper.pdf --force

# Custom output root
python3 ~/mineru_wrapper/mineru_wrapper.py paper.pdf -o /tmp/out
```

Default output root is the current directory (`.`).

## API Server Deployment

Start the minerU FastAPI server for remote PDF parsing:

```bash
# Manual start (foreground)
~/mineru_wrapper/deploy_api.sh

# Custom port
~/mineru_wrapper/deploy_api.sh --port 8000

# Localhost only
~/mineru_wrapper/deploy_api.sh --host 127.0.0.1

# Systemd service (persistent)
sudo cp ~/mineru_wrapper/mineru-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mineru-api
```

The server binds to `0.0.0.0:8001` by default.

## API Usage (for other people)

API server is deployed at **`http://192.168.1.130:8001`** (LAN only, port 8001).

### Via browser
Open http://192.168.1.130:8001/docs → `/file_parse` → "Try it out" → upload PDF → set `backend=pipeline`, `lang_list=["en"]` → Execute.

### Via curl
```bash
curl -s http://192.168.1.130:8001/file_parse \
  -F "files=@paper.pdf" \
  -F "backend=pipeline" \
  -F 'lang_list=["en"]' \
  -F "return_md=true" \
  -o result.json
```

### Via Python client (recommended)
```bash
# Install dependency
pip install httpx

# Sync (upload → wait → save results)
python3 ~/mineru_wrapper/api_client.py paper.pdf http://192.168.1.130:8001

# Async (submit → poll → download)
python3 ~/mineru_wrapper/api_client.py paper.pdf http://192.168.1.130:8001 --async

# Batch directory
python3 ~/mineru_wrapper/api_client.py pdf_dir/ http://192.168.1.130:8001
```

Output is saved to `./parsed/<name>/{paper.md, images/}`.

### Important
- **Must pass `backend=pipeline`** — the default `hybrid-auto-engine` depends on CUDA vLLM (unavailable on ROCm)
- `lang_list=["en"]` for English, `["ch"]` for Chinese
- Server is limited to 1 concurrent request (ROCm stability)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/file_parse` | **Sync** parse (upload PDF, wait for result) |
| `POST` | `/tasks` | **Async** submit task |
| `GET` | `/tasks/{id}` | Query task status |
| `GET` | `/tasks/{id}/result` | Get task result |
| `GET` | `/docs` | Swagger interactive docs |

## Output Structure

```
output_dir/parsed/<name>/
    paper.md           structured Markdown with LaTeX formulas
    images/            extracted figures (JPG, hash filenames)
    image-map.txt      hash → figure label mapping
                       (e.g., a1b2c3d4.jpg → FIG. 1(a))

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
