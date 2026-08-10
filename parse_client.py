#!/usr/bin/env python3
"""MinerU HTTP parse client — one implementation of the /file_parse and
/tasks wire protocols, shared by the HTTP client (api_client.py) and the
router batch runner (batch_parse.py).

One parameter set used to be written out per caller (and batch silently
dropped the formula/table/lang toggles); one timeout policy lived in three
loops. Here: parse_params() is the single request vocabulary, ENDPOINTS and
TIMEOUTS are the single tables, and each protocol step is one function that
raises ParseClientError instead of returning ad-hoc error dicts.

Two real adapters use these: sync httpx (api_client default path) and
asyncio httpx (batch's concurrent runner, api_client's --async). Both are
injectable — pass an httpx.Client/AsyncClient or a transport so tests can
drive the protocols with httpx.MockTransport.
"""

import asyncio
import base64
import time
from pathlib import Path

import httpx

# Wire contract, in one place.
ENDPOINTS = {
    "file_parse": "/file_parse",
    "tasks": "/tasks",
}
TIMEOUTS = {
    "submit": 120.0,       # POST /tasks
    "parse": 600.0,        # POST /file_parse (wait for the whole parse)
    "parse_batch": 900.0,  # unattended batch runs: long PDFs exceed the 600s interactive budget
    "poll_deadline": 600.0,  # how long to poll a task before giving up
    "status": 30.0,        # single GET /tasks/{id}
    "result": 120.0,       # GET /tasks/{id}/result
}
POLL_INTERVAL = 2.0        # seconds between task status polls

# backend=mineru-api's `pipeline` backend — the V100-reliable path both
# tools always use. Return flags are always requested. Never varied by a
# caller, so they are constants, not parameters.
_FORM_ENABLED = "true"
_RETURNERS = {"return_md": _FORM_ENABLED, "return_images": _FORM_ENABLED}


class ParseClientError(RuntimeError):
    """A parse request failed at the wire level (transport, non-2xx, timeout)."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def parse_params(*, lang: str = "en", formula: bool = True, table: bool = True) -> dict:
    """The /file_parse-parameter vocabulary, built exactly once.

    formula/table False send "false" — the same key set a caller would
    send by hand, but with no caller able to drop a toggle silently.
    """
    return {
        "backend": "pipeline",
        "parse_method": "auto",
        "lang_list": [lang],
        "formula_enable": str(formula).lower(),
        "table_enable": str(table).lower(),
        **_RETURNERS,
    }


def _open_files(pdfs: list[Path]) -> list[tuple]:
    """[(name, (upload_name, handle, mime))].

    Callers MUST close every handle from the returned list. A failed open
    becomes ParseClientError (closing what was opened) so the module's
    contract — only ParseClientError escapes — holds for unreadable inputs.
    """
    handles = []
    try:
        for pdf in pdfs:
            handles.append(("files", (pdf.name, open(pdf, "rb"), "application/pdf")))
    except OSError as e:
        for _, (_, fh, _) in handles:
            fh.close()
        raise ParseClientError(f"cannot open input PDF: {e}") from e
    return handles


def _json_body(resp: httpx.Response, method: str) -> dict:
    """resp.json() that also converts a non-JSON 200 into ParseClientError."""
    try:
        return resp.json()
    except ValueError as e:
        raise ParseClientError(
            f"{method}: non-JSON response: {resp.text[:200]}",
            status_code=resp.status_code, body=resp.text[:300]) from e


def _parse_error(method: str, status_code: int, text: str) -> ParseClientError:
    snippet = text[:300]
    return ParseClientError(
        f"{method} {status_code}: {snippet}",
        status_code=status_code, body=snippet)


def file_parse_sync(base_url: str, pdfs: list[Path], params: dict, *,
                    timeout: float | None = None,
                    transport: httpx.BaseTransport | None = None) -> dict:
    """POST <base>/file_parse with one or more PDFs; wait for the result.

    Sync adapter (api_client default path). `transport` injects
    httpx.MockTransport in tests; None = real transport.
    """
    endpoint = base_url.rstrip("/") + ENDPOINTS["file_parse"]
    files = _open_files(pdfs)
    try:
        with httpx.Client(transport=transport) as client:
            try:
                resp = client.post(
                    endpoint, files=files, data=params,
                    timeout=TIMEOUTS["parse"] if timeout is None else timeout)
            except httpx.HTTPError as e:
                raise ParseClientError(f"request failed: {e}") from e
    finally:
        for _, (_, fh, _) in files:
            fh.close()
    if resp.status_code != 200:
        raise _parse_error("file_parse", resp.status_code, resp.text)
    return _json_body(resp, "file_parse")


async def file_parse_async(client: httpx.AsyncClient, base_url: str,
                           pdfs: list[Path], params: dict, *,
                           timeout: float | None = None) -> dict:
    """POST <base>/file_parse on a caller-supplied AsyncClient (batch's
    concurrent runner; also the default of no other adapter)."""
    endpoint = base_url.rstrip("/") + ENDPOINTS["file_parse"]
    files = _open_files(pdfs)
    try:
        try:
            resp = await client.post(
                endpoint, files=files, data=params,
                timeout=TIMEOUTS["parse"] if timeout is None else timeout)
        except httpx.HTTPError as e:
            raise ParseClientError(f"request failed: {e}") from e
    finally:
        for _, (_, fh, _) in files:
            fh.close()
    if resp.status_code != 200:
        raise _parse_error("file_parse", resp.status_code, resp.text)
    return _json_body(resp, "file_parse")


async def submit_task_async(client: httpx.AsyncClient, base_url: str,
                            pdfs: list[Path], params: dict, *,
                            timeout: float | None = None,
                            ) -> tuple[str, str]:
    """POST <base>/tasks; returns (status_url, result_url)."""
    endpoint = base_url.rstrip("/") + ENDPOINTS["tasks"]
    files = _open_files(pdfs)
    try:
        try:
            resp = await client.post(
                endpoint, files=files, data=params,
                timeout=TIMEOUTS["submit"] if timeout is None else timeout)
        except httpx.HTTPError as e:
            raise ParseClientError(f"submit failed: {e}") from e
    finally:
        for _, (_, fh, _) in files:
            fh.close()
    if resp.status_code != 202:
        raise _parse_error("submit", resp.status_code, resp.text)
    payload = _json_body(resp, "submit")
    return payload["status_url"], payload["result_url"]


async def poll_task_async(client: httpx.AsyncClient, status_url: str, *,
                          deadline: float | None = None) -> str:
    """Poll status_url until the task reaches a terminal status.

    Returns "completed" or "failed"; transient errors keep polling. Raises
    ParseClientError when the deadline (default TIMEOUTS['poll_deadline'])
    passes without a terminal status.
    """
    if deadline is None:
        deadline = time.time() + TIMEOUTS["poll_deadline"]
    while time.time() < deadline:
        try:
            resp = await client.get(status_url, timeout=TIMEOUTS["status"])
        except httpx.HTTPError:
            await asyncio.sleep(POLL_INTERVAL)
            continue
        if resp.status_code == 200:
            status = _json_body(resp, "status").get("status")
            if status in ("completed", "failed"):
                return status
        await asyncio.sleep(POLL_INTERVAL)
    raise ParseClientError(f"timed out waiting for task {status_url}")


async def fetch_result_async(client: httpx.AsyncClient, result_url: str, *,
                             timeout: float | None = None) -> dict:
    """GET <result_url> and return the task's result payload."""
    try:
        resp = await client.get(
            result_url, timeout=TIMEOUTS["result"] if timeout is None else timeout)
    except httpx.HTTPError as e:
        raise ParseClientError(f"result fetch failed: {e}") from e
    if resp.status_code != 200:
        raise _parse_error("result", resp.status_code, resp.text)
    return _json_body(resp, "result")


def materialize_results(paper_dir: Path, file_results: dict) -> dict:
    """Write ONE paper's response entry (md_content + base64 images) to disk.

    Returns {"md_written": bool, "md_chars": int, "images_count": int}. A
    per-image base64 decode failure is dropped (only decoded images count);
    the markdown is written only when non-empty. This is the shared
    implementation of the API-response terms api_client and batch_parse used
    to each hand-roll with divergent error handling.
    """
    md_written = False
    md_chars = 0
    md_content = file_results.get("md_content", "")
    if md_content:
        data = md_content.encode("utf-8")
        (paper_dir / "paper.md").write_bytes(data)
        md_written = True
        md_chars = len(data)

    images_count = 0
    images = file_results.get("images", {})
    if images:
        img_dir = paper_dir / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        for name, b64data in images.items():
            try:
                raw = b64data.split(",", 1)[-1]
                (img_dir / name).write_bytes(base64.b64decode(raw))
                images_count += 1
            except Exception:
                pass
    return {"md_written": md_written, "md_chars": md_chars, "images_count": images_count}