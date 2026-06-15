# minerU Wrapper

PDF → structured Markdown parsing on ROCm GPU. Includes a FastAPI server for remote parsing
and `mineru-router` for multi-GPU load balancing.

## Quick Start

```bash
pip install httpx
python3 api_client.py paper.pdf http://<server>:<port>
```

## Docs

- **Full usage**: [mineru_wrapper.md](mineru_wrapper.md)
- **Benchmark results**: [bench_results.md](bench_results.md) — GPU concurrency scaling data
- **Benchmark tool**: `bench_concurrency.py` — reusable concurrency test script
