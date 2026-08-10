# minerU Wrapper

PDF → structured Markdown parsing on NVIDIA CUDA (V100 / cu126). Includes a FastAPI server for remote parsing
and `mineru-router` for multi-GPU load balancing when more than one GPU is present.

## Stack (this host)

- GPU: Tesla V100-PCIE-32GB (sm_70)
- conda env: `mineru` — torch 2.12.1+cu126, mineru 3.4.x
- env script: `~/mineru-cuda/mineru-cuda-env.sh`
- Prefer `-b pipeline` (stable; hybrid/vlm need more VRAM / vLLM)
- Legacy AMD path (archived): `~/archive/mineru-rocm/` + GitHub `duguex/mineru-rocm` (read-only)

## Quick Start

```bash
# Local CLI
python3 ~/mineru_wrapper/mineru_wrapper.py paper.pdf -o /tmp/out

# API (single GPU)
~/mineru_wrapper/deploy.sh api --host 127.0.0.1 --worker-conc 1

# Client
pip install httpx
python3 api_client.py paper.pdf http://127.0.0.1:8001
```

## Docs

- **Full usage**: [mineru_wrapper.md](mineru_wrapper.md)
- **Historical ROCm multi-GPU bench**: [bench_results.md](bench_results.md) (2× Vega 20; not this host)
- **Benchmark tool**: `bench_concurrency.py` — reusable concurrency test script
