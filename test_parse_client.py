#!/usr/bin/env python3
"""Unit tests for parse_client.py — params, file_parse, tasks protocol.

Run:  python3 test_parse_client.py     (or: python3 -m unittest test_parse_client -v)
Drives every protocol step through httpx.MockTransport — no server needed.
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import parse_client  # noqa: E402
from parse_client import (  # noqa: E402
    ParseClientError,
    fetch_result_async,
    file_parse_async,
    file_parse_sync,
    materialize_results,
    parse_params,
    poll_task_async,
    submit_task_async,
)

TASK_URLS = ("http://srv/tasks/42", "http://srv/tasks/42/result")


def make_pdf(where: Path, name: str = "p.pdf") -> Path:
    p = where / name
    p.write_bytes(b"%PDF fake")
    return p


class ParseParamsTest(unittest.TestCase):
    def test_default_shape(self):
        self.assertEqual(parse_params(), {
            "backend": "pipeline",
            "parse_method": "auto",
            "lang_list": ["en"],
            "formula_enable": "true",
            "table_enable": "true",
            "return_md": "true",
            "return_images": "true",
        })

    def test_toggles(self):
        self.assertEqual(parse_params(lang="ch", formula=False, table=False), {
            "backend": "pipeline",
            "parse_method": "auto",
            "lang_list": ["ch"],
            "formula_enable": "false",
            "table_enable": "false",
            "return_md": "true",
            "return_images": "true",
        })


class FileParseTest(unittest.TestCase):
    def test_sync_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/file_parse")
            assert b"p.pdf" in request.read(), "pdf body uploaded"
            return httpx.Response(200, json={"results": {"p": {"ok": True}}})

        with tempfile.TemporaryDirectory() as td:
            pdf = make_pdf(Path(td))
            got = file_parse_sync("http://srv", [pdf], parse_params(),
                                  transport=httpx.MockTransport(handler))
        self.assertEqual(got, {"results": {"p": {"ok": True}}})

    def test_sync_non_200_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        with tempfile.TemporaryDirectory() as td:
            pdf = make_pdf(Path(td))
            with self.assertRaises(ParseClientError) as ctx:
                file_parse_sync("http://srv", [pdf], parse_params(),
                                transport=httpx.MockTransport(handler))
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("boom", str(ctx.exception))

    def test_async_upload_and_success(self):
        done = {}

        def handler(request: httpx.Request) -> httpx.Response:
            done["body"] = request.read()
            return httpx.Response(200, json={"results": {}})

        async def run():
            async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)) as client:
                with tempfile.TemporaryDirectory() as td:
                    pdf = make_pdf(Path(td), "x.pdf")
                    return await file_parse_async(
                        client, "http://srv", [pdf], parse_params())

        got = asyncio.run(run())
        self.assertEqual(got, {"results": {}})
        assert b"x.pdf" in done["body"]


class TasksProtocolTest(unittest.IsolatedAsyncioTestCase):
    def _client(self, handler):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_submit_then_poll_then_fetch(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/tasks":
                return httpx.Response(
                    202, json={"status_url": TASK_URLS[0], "result_url": TASK_URLS[1]})
            if request.url.path == "/tasks/42":
                return httpx.Response(200, json={"status": "completed"})
            if request.url.path == "/tasks/42/result":
                return httpx.Response(200, json={"results": {"p": {}}})
            return httpx.Response(404)

        with tempfile.TemporaryDirectory() as td:
            pdf = make_pdf(Path(td))
            async with self._client(handler) as client:
                status_url, result_url = await submit_task_async(
                    client, "http://srv", [pdf], parse_params())
                self.assertEqual((status_url, result_url), TASK_URLS)
                status = await poll_task_async(client, status_url)
                self.assertEqual(status, "completed")
                result = await fetch_result_async(client, result_url)
                self.assertEqual(result, {"results": {"p": {}}})
        self.assertEqual(calls, ["/tasks", "/tasks/42", "/tasks/42/result"])

    async def test_poll_retries_processing(self):
        old = parse_client.POLL_INTERVAL
        parse_client.POLL_INTERVAL = 0.0  # no real waiting in tests
        try:
            state = {"n": 0}

            def handler(request: httpx.Request) -> httpx.Response:
                state["n"] += 1
                return httpx.Response(
                    200, json={"status": "processing" if state["n"] < 3 else "completed"})

            async with self._client(handler) as client:
                status = await poll_task_async(client, "http://srv/tasks/42")
        finally:
            parse_client.POLL_INTERVAL = old
        self.assertEqual(status, "completed")
        self.assertEqual(state["n"], 3)

    async def test_deadline_passed_raises(self):
        async with self._client(lambda r: httpx.Response(200, json={"status": "processing"})) as client:
            with self.assertRaises(ParseClientError):
                await poll_task_async(client, "http://srv/tasks/42", deadline=time.time() - 1)

    async def test_submit_reject_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="bad request")

        with tempfile.TemporaryDirectory() as td:
            pdf = make_pdf(Path(td))
            async with self._client(handler) as client:
                with self.assertRaises(ParseClientError) as ctx:
                    await submit_task_async(client, "http://srv", [pdf], parse_params())
        self.assertEqual(ctx.exception.status_code, 400)

    async def test_fetch_result_error_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="unavailable")

        async with self._client(handler) as client:
            with self.assertRaises(ParseClientError) as ctx:
                await fetch_result_async(client, "http://srv/tasks/42/result")
        self.assertEqual(ctx.exception.status_code, 503)


class MaterializeResultsTest(unittest.TestCase):
    def test_writes_md_and_images(self):
        import base64
        with tempfile.TemporaryDirectory() as td:
            paper = Path(td)
            saved = materialize_results(paper, {
                "md_content": "# paper\n",
                "images": {
                    "a1.jpg": "data:image/jpeg;base64," + base64.b64encode(b"jpg").decode(),
                    "bad.jpg": "!!not-base64!!",
                },
            })
            self.assertTrue(saved["md_written"])
            self.assertEqual(saved["md_chars"], len("# paper\n".encode("utf-8")))
            self.assertEqual(saved["images_count"], 1, "bad base64 dropped, good one counted")
            self.assertEqual((paper / "paper.md").read_text(), "# paper\n")
            self.assertTrue((paper / "images" / "a1.jpg").exists())
            self.assertFalse((paper / "images" / "bad.jpg").exists())

    def test_empty_entry_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            saved = materialize_results(Path(td), {})
            self.assertEqual(saved, {"md_written": False, "md_chars": 0, "images_count": 0})
            self.assertFalse((Path(td) / "paper.md").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)