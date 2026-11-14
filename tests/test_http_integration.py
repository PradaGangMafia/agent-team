"""End-to-end tests against a local mock OpenAI-compatible server.

These exercise the real HTTP client, the tool loop, multipart image edits and
the chat-based image fallback - no external network access required.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_team as at  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)

MANAGER_MODEL = "mock-manager"
WORKER_MODEL = "mock-worker"


class MockState:
    def __init__(self) -> None:
        self.chat_calls: list[dict] = []
        self.image_edits: list[dict] = []
        self.auth_headers: list[str] = []
        self.request_headers: list[dict] = []
        self.chat_headers: list[dict] = []
        self.image_mode = "images"  # or "chat"
        self.block_mode = ""  # "" or "cloudflare"


class Handler(BaseHTTPRequestHandler):
    state: MockState
    protocol_version = "HTTP/1.1"

    # -- plumbing --------------------------------------------------------
    def log_message(self, *args, **kwargs):  # silence
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _tool(self, name: str, args: dict, call_id: str) -> dict:
        return {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if self.path.endswith("/models"):
            self._send_json({"data": [{"id": MANAGER_MODEL}, {"id": WORKER_MODEL}]})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        body = self._read_body()
        self.state.auth_headers.append(self.headers.get("Authorization", ""))
        self.state.request_headers.append(dict(self.headers.items()))
        if self.state.block_mode == "cloudflare":
            payload = json.dumps(
                {
                    "type": "https://developers.cloudflare.com/support/troubleshooting/"
                    "http-status-codes/cloudflare-1xxx-errors/error-1010/",
                    "title": "Error 1010: Access denied",
                    "status": 403,
                    "detail": "The site owner has blocked access based on your browser's signature.",
                    "error_code": 1010,
                    "error_name": "browser_signature_banned",
                    "cloudflare_error": True,
                    "retryable": False,
                }
            ).encode()
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.endswith("/chat/completions"):
            self._chat(json.loads(body))
        elif self.path.endswith("/images/edits"):
            self._edits(body)
        elif self.path.endswith("/images/generations"):
            self._send_json({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        else:
            self._send_json({"error": f"unknown path {self.path}"}, 404)

    def _chat(self, payload: dict) -> None:
        self.state.chat_calls.append(payload)
        model = payload.get("model")
        self.state.chat_headers.append(
            {"model": model, "headers": {k.lower(): v for k, v in self.headers.items()}}
        )
        messages = payload.get("messages") or []
        assistant_turns = sum(1 for m in messages if m.get("role") == "assistant")

        if self.state.image_mode == "chat":
            text = json.dumps(messages[0].get("content")) if messages else ""
            if "image generation engine" in text:
                data_url = "data:image/png;base64," + base64.b64encode(PNG).decode()
                self._send_json(
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "Here you go",
                                    "images": [{"type": "image_url", "image_url": {"url": data_url}}],
                                }
                            }
                        ]
                    }
                )
                return

        if model == MANAGER_MODEL:
            if assistant_turns == 0:
                call = self._tool(
                    "send_task_to_worker",
                    {"instructions": "Follow the task exactly."},
                    "call_m1",
                )
            elif assistant_turns == 1:
                call = self._tool(
                    "list_dir", {"path": "work", "recursive": True}, "call_m2"
                )
            else:
                call = self._tool(
                    "finish_task",
                    {"verdict": "pass", "notes": "mock manager verified"},
                    "call_m3",
                )
            self._reply_with_tool(call)
            return

        # worker
        if assistant_turns == 0:
            call = self._tool("read_image", {"path": "ref/master.png"}, "call_w1")
        elif assistant_turns == 1:
            call = self._tool(
                "generate_image",
                {
                    "prompt": "same robot, different pose",
                    "output": "work/images/variant_01.png",
                    "references": ["ref/master.png"],
                },
                "call_w2",
            )
        elif assistant_turns == 2:
            call = self._tool(
                "write_log",
                {
                    "content": (
                        "# Task p01 mock log\n\n"
                        "- Inspected ref/master.png\n"
                        "- Generated work/images/variant_01.png from the master\n"
                        "- Verified the file exists\n"
                    )
                },
                "call_w3",
            )
        elif assistant_turns == 3:
            call = self._tool(
                "write_file",
                {"path": "work/notes.md", "content": "generated in the mock integration test\n"},
                "call_w4",
            )
        else:
            call = self._tool(
                "finish_task",
                {"summary": "mock worker done", "status": "done"},
                "call_w5",
            )
        self._reply_with_tool(call)

    def _reply_with_tool(self, call: dict) -> None:
        self._send_json(
            {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [call]}}]}
        )

    def _edits(self, body: bytes) -> None:
        content_type = self.headers.get("Content-Type", "")
        self.state.image_edits.append(
            {
                "content_type": content_type,
                "has_image_field": b'name="image"' in body,
                "has_png": PNG in body or b"\x89PNG" in body,
                "body": body,
            }
        )
        self._send_json({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})


class ServerFixture:
    def __init__(self, image_mode: str = "images", block_mode: str = "") -> None:
        self.state = MockState()
        self.state.image_mode = image_mode
        self.state.block_mode = block_mode

        handler = type("BoundHandler", (Handler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def build_project(
    root: Path,
    base_url: str,
    image_mode: str = "images",
    extra_env: dict[str, str] | None = None,
) -> at.Config:
    for name in ("tasks", "ref", "logs", "work"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "manager.md").write_text("# MANAGER AGENT\n\nDelegate and verify.\n", encoding="utf-8")
    (root / "worker.md").write_text("# WORKER AGENT\n\nDo the work.\n", encoding="utf-8")
    (root / "ref" / "master.png").write_bytes(PNG)
    (root / "tasks" / "p01.md").write_text(
        "---\nid: p01\ntitle: Mock image task\nexpects:\n  - work/images/*.png\n---\n\n"
        "# Mock image task\n\nUse ref/master.png to create one variation.\n",
        encoding="utf-8",
    )
    (root / ".env").write_text(
        f"MANAGER_BASE_URL={base_url}\nMANAGER_API_KEY=test-key\nMANAGER_MODEL={MANAGER_MODEL}\n"
        f"WORKER_BASE_URL={base_url}\nWORKER_API_KEY=test-key\nWORKER_MODEL={WORKER_MODEL}\n"
        f"IMAGE_BASE_URL={base_url}\nIMAGE_API_KEY=test-key\nIMAGE_MODEL=mock-image\n"
        f"IMAGE_MODE={image_mode}\nIMAGE_SIZE=512x512\n"
        + "".join(f"{key}={value}\n" for key, value in (extra_env or {}).items()),
        encoding="utf-8",
    )
    cfg = at.Config.load(root)
    cfg.root = root
    cfg.tasks_dir = root / "tasks"
    cfg.ref_dir = root / "ref"
    cfg.logs_dir = root / "logs"
    cfg.work_dir = root / "work"
    cfg.manager_prompt = root / "manager.md"
    cfg.worker_prompt = root / "worker.md"
    return cfg


class TestHttpIntegration(unittest.TestCase):
    def _run(
        self,
        image_mode: str = "images",
        block_mode: str = "",
        extra_env: dict[str, str] | None = None,
    ):
        # .env values land in os.environ; keep every test isolated.
        saved_env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved_env)))
        for key in [
            k
            for k in os.environ
            if k.split("_")[0] in {"MANAGER", "WORKER", "IMAGE", "HTTP", "AGENT"}
        ]:
            os.environ.pop(key, None)

        server = ServerFixture(image_mode, block_mode)
        self.addCleanup(server.stop)
        tmp = tempfile.TemporaryDirectory(prefix="agent-team-http-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        cfg = build_project(root, server.base_url, image_mode, extra_env)
        reporter = at.Reporter(cfg.logs_dir, quiet=True)
        orchestrator = at.Orchestrator(cfg, reporter, at.ClientFactory(cfg, reporter))
        outcomes = orchestrator.run(at.discover_tasks(cfg))
        return outcomes, root, cfg, server.state

    def test_images_api_edits_flow(self) -> None:
        outcomes, root, cfg, state = self._run("images")
        self.assertEqual([o.status for o in outcomes], ["done"], outcomes[0].notes)

        # manager + worker both talked to their own model
        models = {call["model"] for call in state.chat_calls}
        self.assertEqual(models, {MANAGER_MODEL, WORKER_MODEL})
        self.assertTrue(all(h.startswith("Bearer test-key") for h in state.auth_headers))

        # image-to-image went through /v1/images/edits as multipart with the master
        self.assertEqual(len(state.image_edits), 1)
        edit = state.image_edits[0]
        self.assertIn("multipart/form-data", edit["content_type"])
        self.assertTrue(edit["has_image_field"])
        self.assertTrue(edit["has_png"])
        self.assertIn(b"same robot, different pose", edit["body"])

        # artefacts and log landed on disk
        variant = root / "work" / "images" / "variant_01.png"
        self.assertEqual(variant.read_bytes(), PNG)
        self.assertTrue((root / "work" / "notes.md").is_file())
        log = (root / "logs" / "p01.log").read_text(encoding="utf-8")
        self.assertIn("mock log", log)
        self.assertIn("variant_01.png", log)

    def test_chat_image_fallback(self) -> None:
        outcomes, root, cfg, state = self._run("chat")
        self.assertEqual([o.status for o in outcomes], ["done"], outcomes[0].notes)
        self.assertEqual(state.image_edits, [])
        self.assertEqual((root / "work" / "images" / "variant_01.png").read_bytes(), PNG)

        # the prompt must survive even when optional size/quality are blank
        image_requests = [
            call
            for call in state.chat_calls
            if "image generation engine" in json.dumps(call["messages"][0].get("content"))
        ]
        self.assertTrue(image_requests)
        prompt_text = json.dumps(image_requests[0]["messages"][1])
        self.assertIn("same robot, different pose", prompt_text)
        self.assertIn("Return the generated image", prompt_text)

    def test_worker_receives_attached_reference_image(self) -> None:
        outcomes, root, cfg, state = self._run("images")
        self.assertEqual(outcomes[0].status, "done")
        # a follow-up worker request must carry the attached image as a data URL
        worker_calls = [c for c in state.chat_calls if c["model"] == WORKER_MODEL]
        self.assertTrue(
            any("data:image/png;base64," in json.dumps(c) for c in worker_calls[1:]),
            "reference image was not attached to the worker conversation",
        )


    def test_requests_look_like_a_real_browser(self) -> None:
        outcomes, root, cfg, state = self._run("images")
        self.assertEqual([o.status for o in outcomes], ["done"], outcomes[0].notes)
        agents = [h.get("User-Agent", "") for h in state.request_headers]
        self.assertTrue(agents)
        for agent in agents:
            self.assertIn("Mozilla/5.0", agent)
            self.assertIn("Chrome/", agent)
            self.assertNotIn("Python-urllib", agent)
        # browser-ish extras are present too
        first = {k.lower(): v for k, v in state.request_headers[0].items()}
        self.assertIn("accept-language", first)
        self.assertIn("sec-ch-ua", first)

    def test_custom_headers_from_env(self) -> None:
        outcomes, root, cfg, state = self._run(
            "images",
            extra_env={
                "HTTP_HEADERS_JSON": '{"X-Global": "everywhere"}',
                "WORKER_HEADERS_JSON": '{"X-Agent": "worker-only"}',
                "HTTP_USER_AGENT": "MyCustomAgent/9.9",
            },
        )
        self.assertEqual([o.status for o in outcomes], ["done"], outcomes[0].notes)

        lowered = [{k.lower(): v for k, v in h.items()} for h in state.request_headers]
        self.assertTrue(all(h.get("x-global") == "everywhere" for h in lowered))

        worker_requests = [
            entry["headers"] for entry in state.chat_headers if entry["model"] == WORKER_MODEL
        ]
        manager_requests = [
            entry["headers"] for entry in state.chat_headers if entry["model"] == MANAGER_MODEL
        ]
        self.assertTrue(worker_requests and manager_requests)
        self.assertTrue(all(h.get("x-agent") == "worker-only" for h in worker_requests))
        self.assertTrue(all("x-agent" not in h for h in manager_requests))
        self.assertTrue(all(h.get("user-agent") == "MyCustomAgent/9.9" for h in lowered))

    def test_cloudflare_block_reports_actionable_error(self) -> None:
        server = ServerFixture("images", block_mode="cloudflare")
        self.addCleanup(server.stop)
        tmp = tempfile.TemporaryDirectory(prefix="agent-team-cf-")
        self.addCleanup(tmp.cleanup)

        saved_env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved_env)))
        for key in [k for k in os.environ if k.split("_")[0] in {"MANAGER", "WORKER", "IMAGE", "HTTP"}]:
            os.environ.pop(key, None)

        root = Path(tmp.name)
        cfg = build_project(root, server.base_url, "images")
        reporter = at.Reporter(cfg.logs_dir, quiet=True)
        orchestrator = at.Orchestrator(cfg, reporter, at.ClientFactory(cfg, reporter))

        with self.assertRaises(at.EndpointError) as ctx:
            orchestrator.run(at.discover_tasks(cfg))

        message = str(ctx.exception)
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("Cloudflare", message)
        self.assertIn("curl_cffi", message)
        self.assertIn("HTTP_USER_AGENT", message)
        # error 1010 is explicitly not retryable: exactly one request must be sent
        self.assertEqual(len(server.state.request_headers), 1)

    @unittest.skipUnless(
        at.http_backend() == "curl_cffi",
        "curl_cffi not installed (pip install curl_cffi to test browser impersonation)",
    )
    def test_curl_cffi_backend_when_installed(self) -> None:
        outcomes, root, cfg, state = self._run("images")
        self.assertEqual([o.status for o in outcomes], ["done"], outcomes[0].notes)
        agents = [h.get("User-Agent", "") for h in state.request_headers]
        self.assertTrue(all("Chrome/" in a for a in agents))
        self.assertTrue(all("python" not in a.lower() for a in agents))


if __name__ == "__main__":
    unittest.main(verbosity=2)
