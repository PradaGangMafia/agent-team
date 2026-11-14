#!/usr/bin/env python3
"""
Agent-Team
==========

A small, dependency-free Python workflow runner that drives two independent
LLM agents over OpenAI-compatible ``/v1`` endpoints:

  MANAGER  reads ``tasks/`` in order, hands each task to the WORKER, waits for
           it to finalise, then verifies that the task log exists in ``logs/``
           and that work landed in ``work/`` before moving to the next task.

  WORKER   loads reference files from ``ref/`` (images, code, documents...),
           executes the task with file / image / shell tools, writes a detailed
           log to ``logs/<task id>.log`` and stores every artefact in ``work/``.

The two agents each have their own base URL, API key, model and sampling
configuration (see ``example.env``), so the manager can be a cheap model and
the worker a strong one - or two different providers entirely.

Requests identify as a real desktop browser (user agent, client hints, and -
with the optional ``curl_cffi`` package - a matching TLS fingerprint), because
Cloudflare's bot protection rejects the default Python signature with
"Error 1010: browser_signature_banned". See ``example.env`` for
``HTTP_BACKEND`` / ``HTTP_USER_AGENT`` / ``*_HEADERS_JSON``.

Image work is first class: the worker can generate images from one or many
master images through the OpenAI-compatible ``/v1/images/edits`` and
``/v1/images/generations`` endpoints, with a multimodal ``/v1/chat/completions``
fallback for providers that return images in chat responses.

Usage
-----
    cp example.env .env          # fill in endpoints + keys
    python agent_team.py doctor  # health check
    python agent_team.py run     # run the whole queue
    python agent_team.py run --tasks p01,p02 --max-tasks 2
    python agent_team.py run --simulate   # offline dry run, no keys needed

Layout expected under the project root (defaults to the script's directory):

    agent_team.py
    manager.md                      manager agent prompt
    worker.md                       worker agent prompt
    example.env
    tasks/p01.md, p02.md, ...       the work queue, executed in order
    ref/                            reference files for the tasks
    logs/                           per-task logs + run logs
    work/                           everything the worker produces

Only the Python standard library is used, so there is nothing to install.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import mimetypes
import os
import random
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

__version__ = "1.1.0"

# ---------------------------------------------------------------------------
# Small helpers: console output, ids, natural sorting
# ---------------------------------------------------------------------------

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


def green(t: str) -> str:
    return _c("32", t)


def yellow(t: str) -> str:
    return _c("33", t)


def red(t: str) -> str:
    return _c("31", t)


def cyan(t: str) -> str:
    return _c("36", t)


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def short(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def natural_key(path: Path) -> list[Any]:
    """Sort p1, p2, p10 in human order instead of lexicographic order."""
    parts = re.split(r"(\d+)", path.name.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n = float(n) / 1024.0  # type: ignore[assignment]
    return f"{n:.1f}GB"


class Reporter:
    """Console output mirrored into a run log under ``logs/``."""

    def __init__(self, logs_dir: Path, verbose: bool = False, quiet: bool = False):
        self.verbose = verbose
        self.quiet = quiet
        self.lines: list[str] = []
        logs_dir.mkdir(parents=True, exist_ok=True)
        self.path = logs_dir / f"run-{run_stamp()}.log"

    def _record(self, plain: str) -> None:
        self.lines.append(plain)
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(plain + "\n")
        except OSError:
            pass

    def raw(self, text: str) -> None:
        self._record(re.sub(r"\033\[[0-9;]*m", "", text))
        if not self.quiet:
            print(text, flush=True)

    def event(self, tag: str, text: str) -> None:
        self.raw(f"{dim(now_stamp())} {dim('[' + tag + ']')} {text}")

    def info(self, text: str) -> None:
        self.event("info", text)

    def ok(self, text: str) -> None:
        self.event(green("ok"), text)

    def warn(self, text: str) -> None:
        self.event(yellow("warn"), yellow(text))

    def error(self, text: str) -> None:
        self.event(red("error"), red(text))

    def debug(self, text: str) -> None:
        if self.verbose:
            self.event(dim("debug"), dim(text))

    def banner(self, title: str) -> None:
        self.raw("")
        self.raw(bold(cyan("=" * 72)))
        self.raw(bold(cyan(f"  {title}")))
        self.raw(bold(cyan("=" * 72)))


# ---------------------------------------------------------------------------
# .env loading and configuration
# ---------------------------------------------------------------------------


def load_dotenv(path: Path) -> None:
    """Minimal .env loader; real environment variables always win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            # strip trailing inline comment for unquoted values
            value = re.sub(r"\s+#.*$", "", value)
        os.environ.setdefault(key, value)


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    raw = env_str(name)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = env_str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on", "enabled"}


def env_json(name: str) -> dict[str, Any]:
    raw = env_str(name)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


@dataclass
class AgentConfig:
    """Everything needed to talk to one OpenAI-compatible agent endpoint."""

    role: str
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.4
    max_tokens: int = 4096
    timeout: float = 300.0
    extra_body: dict[str, Any] = field(default_factory=dict)
    tokens_param: str = "max_tokens"
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    @classmethod
    def from_env(cls, prefix: str, fallback: "AgentConfig | None" = None) -> "AgentConfig":
        fb = fallback
        return cls(
            role=prefix.lower(),
            base_url=env_str(f"{prefix}_BASE_URL", fb.base_url if fb else ""),
            api_key=env_str(f"{prefix}_API_KEY", fb.api_key if fb else ""),
            model=env_str(f"{prefix}_MODEL", fb.model if fb else ""),
            temperature=env_float(f"{prefix}_TEMPERATURE", fb.temperature if fb else 0.4),
            max_tokens=env_int(f"{prefix}_MAX_TOKENS", fb.max_tokens if fb else 4096),
            timeout=env_float(f"{prefix}_TIMEOUT", fb.timeout if fb else 300.0),
            extra_body=env_json(f"{prefix}_EXTRA_JSON")
            or (dict(fb.extra_body) if fb else {}),
            tokens_param=env_str(f"{prefix}_TOKENS_PARAM", fb.tokens_param if fb else "max_tokens")
            or "max_tokens",
            headers={
                str(k): str(v)
                for k, v in (env_json(f"{prefix}_HEADERS_JSON") or dict(fb.headers if fb else {})).items()
            },
        )

    def masked(self) -> str:
        if not self.api_key:
            return "(none)"
        if len(self.api_key) <= 8:
            return "*" * len(self.api_key)
        return self.api_key[:4] + "…" + self.api_key[-4:]

    def is_local(self) -> bool:
        host = urllib.parse.urlparse(self.base_url).hostname or ""
        return host in {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}


@dataclass
class ImageConfig:
    """Config for the OpenAI-compatible /v1/images endpoints."""

    base_url: str
    api_key: str
    model: str
    mode: str = "auto"  # auto | images | chat
    size: str = ""
    quality: str = ""
    edit_field: str = "image"
    response_format: str = ""
    timeout: float = 600.0
    chat_fallback: bool = True
    keep_chat_model: str = ""
    max_ref_bytes: int = 20 * 1024 * 1024
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def generate_endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/images/generations"

    @property
    def edit_endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/images/edits"

    @classmethod
    def from_env(cls, worker: AgentConfig) -> "ImageConfig":
        base = env_str("IMAGE_BASE_URL") or worker.base_url
        key = env_str("IMAGE_API_KEY") or worker.api_key
        model = env_str("IMAGE_MODEL", "")
        mode = env_str("IMAGE_MODE", "auto").lower() or "auto"
        if mode not in {"auto", "images", "chat"}:
            mode = "auto"
        return cls(
            base_url=base,
            api_key=key,
            model=model,
            mode=mode,
            size=env_str("IMAGE_SIZE", ""),
            quality=env_str("IMAGE_QUALITY", ""),
            edit_field=env_str("IMAGE_EDIT_FIELD", "image") or "image",
            response_format=env_str("IMAGE_RESPONSE_FORMAT", ""),
            timeout=env_float("IMAGE_TIMEOUT", 600.0),
            chat_fallback=env_bool("IMAGE_CHAT_FALLBACK", True),
            keep_chat_model=env_str("IMAGE_CHAT_MODEL", ""),
            headers={str(k): str(v) for k, v in env_json("IMAGE_HEADERS_JSON").items()},
        )

    def masked(self) -> str:
        if not self.api_key:
            return "(none)"
        return self.api_key[:4] + "…" + self.api_key[-4:] if len(self.api_key) > 8 else "***"


@dataclass
class Config:
    root: Path
    manager: AgentConfig
    worker: AgentConfig
    image: ImageConfig
    tasks_dir: Path
    ref_dir: Path
    logs_dir: Path
    work_dir: Path
    manager_prompt: Path
    worker_prompt: Path
    max_rounds_manager: int = 20
    max_rounds_worker: int = 40
    max_task_attempts: int = 2
    min_log_bytes: int = 120
    require_work_output: bool = True
    continue_on_failure: bool = False
    allow_shell: bool = True
    shell_timeout: float = 300.0
    tool_result_max_chars: int = 24000
    max_image_bytes: int = 8 * 1024 * 1024
    simulate: bool = False
    verbose: bool = False
    quiet: bool = False

    @classmethod
    def load(cls, root: Path) -> "Config":
        root = root.resolve()
        load_dotenv(root / ".env")

        tasks_dir = root / env_str("TASKS_DIR", "tasks")
        ref_dir = root / env_str("REF_DIR", "ref")
        logs_dir = root / env_str("LOGS_DIR", "logs")
        work_dir = root / env_str("WORK_DIR", "work")

        manager = AgentConfig.from_env("MANAGER")
        worker = AgentConfig.from_env("WORKER")
        # Reasonable fallbacks so a single endpoint can drive both agents.
        if not manager.base_url:
            manager.base_url = env_str("OPENAI_BASE_URL", worker.base_url)
            manager.api_key = manager.api_key or env_str("OPENAI_API_KEY", worker.api_key)
        if not worker.base_url:
            worker.base_url = env_str("OPENAI_BASE_URL", manager.base_url)
            worker.api_key = worker.api_key or env_str("OPENAI_API_KEY", manager.api_key)
        if not manager.model:
            manager.model = env_str("MANAGER_MODEL", "gpt-4o-mini")
        if not worker.model:
            worker.model = env_str("WORKER_MODEL", "gpt-4o")

        return cls(
            root=root,
            manager=manager,
            worker=worker,
            image=ImageConfig.from_env(worker),
            tasks_dir=tasks_dir,
            ref_dir=ref_dir,
            logs_dir=logs_dir,
            work_dir=work_dir,
            manager_prompt=root / env_str("MANAGER_PROMPT", "manager.md"),
            worker_prompt=root / env_str("WORKER_PROMPT", "worker.md"),
            max_rounds_manager=max(1, env_int("MANAGER_MAX_ROUNDS", 20)),
            max_rounds_worker=max(1, env_int("WORKER_MAX_ROUNDS", 40)),
            max_task_attempts=max(1, env_int("MAX_TASK_ATTEMPTS", 2)),
            min_log_bytes=env_int("MIN_LOG_BYTES", 120),
            require_work_output=env_bool("REQUIRE_WORK_OUTPUT", True),
            continue_on_failure=env_bool("CONTINUE_ON_FAILURE", False),
            allow_shell=env_bool("ALLOW_SHELL", True),
            shell_timeout=env_float("SHELL_TIMEOUT", 300.0),
            tool_result_max_chars=env_int("TOOL_RESULT_MAX_CHARS", 24000),
            max_image_bytes=env_int("MAX_IMAGE_BYTES", 8 * 1024 * 1024),
            simulate=env_bool("AGENT_TEAM_SIMULATE", False),
        )

    # -- derived ---------------------------------------------------------
    def missing_dirs(self) -> list[Path]:
        return [
            p
            for p in (self.tasks_dir, self.ref_dir, self.logs_dir, self.work_dir)
            if not p.is_dir()
        ]

    def ensure_dirs(self) -> None:
        for p in (self.tasks_dir, self.ref_dir, self.logs_dir, self.work_dir):
            p.mkdir(parents=True, exist_ok=True)

    def describe(self) -> str:
        w = 18
        lines = [
            f"{'root'.ljust(w)} {self.root}",
            f"{'tasks'.ljust(w)} {self.tasks_dir}",
            f"{'ref'.ljust(w)} {self.ref_dir}",
            f"{'logs'.ljust(w)} {self.logs_dir}",
            f"{'work'.ljust(w)} {self.work_dir}",
            "",
            f"{'manager'.ljust(w)} {self.manager.model} @ {self.manager.base_url or '(unset)'}",
            f"{'manager key'.ljust(w)} {self.manager.masked()}",
            f"{'worker'.ljust(w)} {self.worker.model} @ {self.worker.base_url or '(unset)'}",
            f"{'worker key'.ljust(w)} {self.worker.masked()}",
            f"{'images'.ljust(w)} {self.image.model or '(unset)'} @ "
            f"{self.image.base_url or '(unset)'} mode={self.image.mode}",
            "",
            f"{'attempts'.ljust(w)} {self.max_task_attempts}",
            f"{'worker rounds'.ljust(w)} {self.max_rounds_worker}",
            f"{'manager rounds'.ljust(w)} {self.max_rounds_manager}",
            f"{'shell tools'.ljust(w)} {'enabled' if self.allow_shell else 'disabled'}",
            f"{'simulate'.ljust(w)} {'yes' if self.simulate else 'no'}",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class EndpointError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 522, 524}

# A current desktop Chrome UA. Python's default "Python-urllib/3.x" is exactly
# what Cloudflare's bot protection (error 1010, "browser signature banned")
# rejects, so every request now looks like a browser unless configured otherwise.
DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CF_MARKERS = (
    "cloudflare",
    "error 1010",
    "error 1020",
    "browser_signature_banned",
    "browser signature",
    "cf-mitigated",
    "just a moment",
    "attention required",
    "checking your browser",
)

CF_HINT = (
    "Cloudflare blocked this request. Errors 1010/1020 and 'Just a moment' pages are "
    "bot-protection rejections of the client signature: retrying will not help.\n"
    "  Fixes, best first:\n"
    "  1. pip install curl_cffi   (agent_team.py then impersonates Chrome's TLS/JA3 "
    "fingerprint, not just its headers - this is what defeats error 1010; force it with "
    "HTTP_BACKEND=curl_cffi)\n"
    "  2. Point HTTP_USER_AGENT at your real browser UA, and/or pass cookies via "
    "HTTP_HEADERS_JSON='{\"Cookie\": \"cf_clearance=...\"}'\n"
    "  3. If Cloudflare Access fronts the endpoint, add CF-Access-Client-Id / "
    "CF-Access-Client-Secret through WORKER_HEADERS_JSON / MANAGER_HEADERS_JSON\n"
    "  4. Or exempt this endpoint from bot protection in the Cloudflare dashboard "
    "(WAF custom rule / IP allowlist)"
)

_curl_module: Any | None = None
_curl_checked = False


def _curl_cffi() -> Any | None:
    """Return the optional curl_cffi.requests module, or None."""
    global _curl_module, _curl_checked
    if not _curl_checked:
        _curl_checked = True
        try:  # optional dependency: browser-grade TLS fingerprint
            from curl_cffi import requests as curl_requests  # type: ignore

            _curl_module = curl_requests
        except Exception:
            _curl_module = None
    return _curl_module


def http_backend() -> str:
    """Which transport to use: 'curl_cffi' (browser impersonation) or 'urllib'."""
    choice = (env_str("HTTP_BACKEND", "auto") or "auto").lower()
    if choice in {"urllib", "python", "stdlib"}:
        return "urllib"
    available = _curl_cffi() is not None
    if choice in {"curl_cffi", "curl-cffi", "browser", "impersonate"}:
        return "curl_cffi" if available else "urllib"
    if choice not in {"auto", ""}:
        return "curl_cffi" if available else "urllib"
    return "curl_cffi" if available else "urllib"


def browser_headers() -> dict[str, str]:
    """Browser-like request headers, overridable from the environment."""
    globals_extra = {str(k): str(v) for k, v in env_json("HTTP_HEADERS_JSON").items()}
    if not env_bool("HTTP_BROWSER_HEADERS", True):
        headers: dict[str, str] = {"Accept": "application/json, text/plain, */*"}
        ua = env_str("HTTP_USER_AGENT")
        if ua:
            headers["User-Agent"] = ua
        headers.update(globals_extra)
        return headers

    ua = env_str("HTTP_USER_AGENT") or DEFAULT_BROWSER_UA
    headers = {
        "User-Agent": ua,
        "Accept": env_str("HTTP_ACCEPT") or "application/json, text/plain, */*",
        "Accept-Language": env_str("HTTP_ACCEPT_LANGUAGE") or "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    version = re.search(r"Chrome/(\d+)", ua)
    if version:
        v = version.group(1)
        headers.update(
            {
                "Sec-Ch-Ua": f'"Chromium";v="{v}", "Not_A Brand";v="24", '
                f'"Google Chrome";v="{v}"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
            }
        )
    headers.update(globals_extra)
    return headers


def _auth_headers(api_key: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Browser headers + per-agent overrides + bearer token."""
    headers = browser_headers()
    for key, value in (extra or {}).items():
        headers[str(key)] = str(value)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _looks_like_cloudflare(status: int | None, body: str, content_type: str = "") -> bool:
    haystack = f"{body}\n{content_type}".lower()
    if any(marker in haystack for marker in CF_MARKERS):
        return True
    return status in {403, 429, 503} and "text/html" in content_type.lower()


def _http_error(url: str, status: int, body: str, content_type: str = "") -> EndpointError:
    message = f"HTTP {status} from {url}: {short(body, 600)}"
    if _looks_like_cloudflare(status, body, content_type):
        message += "\n" + CF_HINT
    return EndpointError(message, status, body)


def _cloudflare_error(url: str, body: str) -> EndpointError:
    return EndpointError(
        f"{url} answered with a Cloudflare bot-protection page instead of JSON "
        f"({short(body, 200)}).\n{CF_HINT}"
    )


def _raw_request(
    url: str,
    data: bytes | None,
    headers: dict[str, str],
    method: str,
    timeout: float,
) -> tuple[int, bytes, str, Any]:
    """One HTTP exchange -> (status, body, content_type, response_headers)."""
    if http_backend() == "curl_cffi":
        module = _curl_cffi()
        if module is not None:
            response = module.request(
                method,
                url,
                data=data,
                headers=headers,
                timeout=timeout,
                impersonate=env_str("HTTP_IMPERSONATE", "chrome") or "chrome",
                allow_redirects=True,
            )
            return (
                int(response.status_code),
                bytes(response.content),
                response.headers.get("Content-Type", ""),
                response.headers,
            )

    request = urllib.request.Request(url, data=data, method=method)
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (
                int(getattr(response, "status", 200) or 200),
                response.read(),
                response.headers.get("Content-Type", ""),
                response.headers,
            )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        return (
            int(exc.code),
            body,
            (exc.headers.get("Content-Type", "") if exc.headers else ""),
            exc.headers or {},
        )


def _request_bytes(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str = "POST",
    timeout: float = 300.0,
    retries: int = 4,
) -> tuple[bytes, str]:
    """POST/GET with retry + backoff. Returns (body, content_type)."""
    attempts = max(1, retries)
    last_error: EndpointError | None = None
    for attempt in range(attempts):
        try:
            status, raw, content_type, response_headers = _raw_request(
                url, data, dict(headers or {}), method, timeout
            )
        except Exception as exc:  # network/DNS/TLS/timeouts from either backend
            last_error = EndpointError(f"network error talking to {url}: {exc}")
            if attempt == attempts - 1:
                raise last_error
            time.sleep(min(2 ** attempt, 20) + random.uniform(0, 0.5))
            continue

        if status < 400:
            return raw, content_type

        body = raw.decode("utf-8", "replace")
        last_error = _http_error(url, status, body, content_type)
        if status not in RETRY_STATUS or attempt == attempts - 1:
            raise last_error
        retry_after = None
        try:
            retry_after = response_headers.get("Retry-After") if response_headers else None
        except Exception:
            retry_after = None
        delay = (
            float(retry_after)
            if (retry_after or "").replace(".", "", 1).isdigit()
            else min(2 ** attempt, 20)
        )
        time.sleep(delay + random.uniform(0, 0.5))
    raise last_error or EndpointError(f"request to {url} failed")


def _multipart(
    fields: dict[str, Any],
    files: Sequence[tuple[str, str, bytes, str]],
) -> tuple[bytes, str]:
    """Build a multipart/form-data body. ``files`` = (field, filename, data, mime)."""
    boundary = "----agentteam" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        if value is None or value == "":
            continue
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += str(value).encode() + b"\r\n"
    for name, filename, data, mime in files:
        out += f"--{boundary}\r\n".encode()
        out += (
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        ).encode()
        out += f"Content-Type: {mime}\r\n\r\n".encode()
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _text_of(content: Any) -> str:
    """Flatten OpenAI-style message content (str or list of parts) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    chunks.append(part["text"])
                elif part.get("type") in {"image_url", "output_image", "image"}:
                    chunks.append("[image]")
        return "\n".join(c for c in chunks if c)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return str(content)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                value = json.loads(match.group(0))
                return value if isinstance(value, dict) else {}
            except json.JSONDecodeError:
                pass
    return {}


# ---------------------------------------------------------------------------
# Chat client
# ---------------------------------------------------------------------------


class ChatClient:
    """Minimal OpenAI-compatible chat completions client."""

    def __init__(self, cfg: AgentConfig, reporter: Reporter | None = None):
        self.cfg = cfg
        self.reporter = reporter
        self.calls = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            self.cfg.tokens_param: self.cfg.max_tokens if max_tokens is None else max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        payload.update(self.cfg.extra_body)

        body = json.dumps(payload).encode("utf-8")
        headers = _auth_headers(self.cfg.api_key, self.cfg.headers)
        headers["Content-Type"] = "application/json"

        self.calls += 1
        started = time.time()
        raw, response_type = _request_bytes(
            self.cfg.endpoint,
            data=body,
            headers=headers,
            timeout=timeout or self.cfg.timeout,
        )
        elapsed = time.time() - started
        text = raw.decode("utf-8", "replace")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            if _looks_like_cloudflare(None, text, response_type):
                raise _cloudflare_error(self.cfg.endpoint, text) from exc
            raise EndpointError(
                f"{self.cfg.role}: response from {self.cfg.endpoint} was not JSON: "
                f"{short(text, 400)}"
            ) from exc

        choices = data.get("choices") or []
        if not choices:
            raise EndpointError(
                f"{self.cfg.role}: no choices returned by {self.cfg.endpoint}: "
                f"{short(json.dumps(data), 400)}"
            )
        message = choices[0].get("message") or {}
        usage = data.get("usage") or {}
        if self.reporter:
            self.reporter.debug(
                f"{self.cfg.role} model={self.cfg.model} {elapsed:.1f}s "
                f"tokens={usage.get('prompt_tokens', '?')}/"
                f"{usage.get('completion_tokens', '?')}"
            )
        return message


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------


class ImageService:
    """Image generation through /v1/images/* with a chat-completions fallback."""

    def __init__(
        self,
        cfg: ImageConfig,
        chat: AgentConfig | None = None,
        reporter: Reporter | None = None,
    ):
        self.cfg = cfg
        self.chat_cfg = chat
        self.reporter = reporter

    # -- public ----------------------------------------------------------
    def generate(
        self,
        prompt: str,
        output: Path,
        references: Sequence[Path] = (),
        *,
        size: str | None = None,
        quality: str | None = None,
        count: int = 1,
    ) -> tuple[list[Path], str]:
        """Create ``count`` images and return (saved paths, backend used)."""
        refs = [p for p in references if p.is_file()]
        count = max(1, int(count))
        size = (size or self.cfg.size or "").strip()
        quality = (quality or self.cfg.quality or "").strip()

        mode = self.cfg.mode
        if mode == "chat":
            return self._via_chat(prompt, output, refs, count, size, quality), "chat"
        if mode == "images":
            return self._via_images(prompt, output, refs, count, size, quality), "images"

        # auto
        try:
            return self._via_images(prompt, output, refs, count, size, quality), "images"
        except EndpointError as exc:
            fallback_ok = (
                self.cfg.chat_fallback
                and self.chat_cfg is not None
                and exc.status in {400, 404, 405, 415, 422, 500, 501}
            )
            if not fallback_ok:
                raise
            if self.reporter:
                self.reporter.warn(
                    f"images API rejected the request ({exc.status}); "
                    "falling back to multimodal /chat/completions"
                )
            return self._via_chat(prompt, output, refs, count, size, quality), "chat"

    # -- images API ------------------------------------------------------
    def _via_images(
        self,
        prompt: str,
        output: Path,
        refs: Sequence[Path],
        count: int,
        size: str,
        quality: str,
    ) -> list[Path]:
        if not self.cfg.model:
            raise EndpointError(
                "IMAGE_MODEL is not set - set it in .env (e.g. gpt-image-1, dall-e-3, "
                "or whatever your endpoint exposes)."
            )
        fields: dict[str, Any] = {
            "model": self.cfg.model,
            "prompt": prompt,
            "n": count,
            "size": size,
            "quality": quality,
            "response_format": self.cfg.response_format,
        }
        headers = _auth_headers(self.cfg.api_key, self.cfg.headers)

        if refs:
            files: list[tuple[str, str, bytes, str]] = []
            for ref in refs:
                if ref.stat().st_size > self.cfg.max_ref_bytes:
                    raise EndpointError(f"reference image too large: {ref}")
                mime = mimetypes.guess_type(ref.name)[0] or "image/png"
                files.append((self.cfg.edit_field, ref.name, ref.read_bytes(), mime))
            body, content_type = _multipart(fields, files)
            headers["Content-Type"] = content_type
            raw, _ = _request_bytes(
                self.cfg.edit_endpoint,
                data=body,
                headers=headers,
                timeout=self.cfg.timeout,
            )
        else:
            headers["Content-Type"] = "application/json"
            clean = {k: v for k, v in fields.items() if v not in ("", None)}
            if not self.cfg.response_format and self.cfg.model.startswith("gpt-image"):
                clean.pop("response_format", None)
            raw, _ = _request_bytes(
                self.cfg.generate_endpoint,
                data=json.dumps(clean).encode("utf-8"),
                headers=headers,
                timeout=self.cfg.timeout,
            )
        return self._save_images(raw, output, count, "images")

    # -- chat fallback ---------------------------------------------------
    def _via_chat(
        self,
        prompt: str,
        output: Path,
        refs: Sequence[Path],
        count: int,
        size: str,
        quality: str,
    ) -> list[Path]:
        cfg = self.chat_cfg
        if cfg is None:
            raise EndpointError("no chat endpoint configured for image fallback")
        model = self.cfg.keep_chat_model or cfg.model
        saved: list[Path] = []
        for index in range(count):
            prompt_lines = [prompt]
            if count > 1:
                prompt_lines.append(f"Variation {index + 1} of {count}.")
            if refs:
                prompt_lines.append("Use the attached reference image(s) as the master.")
            if size:
                prompt_lines.append(f"Requested size: {size}.")
            if quality:
                prompt_lines.append(f"Requested quality: {quality}.")
            prompt_lines.append("Return the generated image.")
            parts: list[dict[str, Any]] = [
                {"type": "text", "text": "\n".join(prompt_lines)}
            ]
            for ref in refs:
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url(ref)},
                    }
                )
            payload: dict[str, Any] = {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are an image generation engine. Produce the requested "
                            "image and return it as an image (image_url / images field / "
                            "data URL / markdown image link)."
                        ),
                    },
                    {"role": "user", "content": parts},
                ],
                "modalities": ["image", "text"],
                "temperature": cfg.temperature,
            }
            payload.update(cfg.extra_body)
            try:
                raw, _ = _request_bytes(
                    cfg.endpoint,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        **_auth_headers(cfg.api_key, cfg.headers),
                        "Content-Type": "application/json",
                    },
                    timeout=max(self.cfg.timeout, cfg.timeout),
                )
            except EndpointError:
                # Some providers reject "modalities"; retry without it.
                payload.pop("modalities", None)
                raw, _ = _request_bytes(
                    cfg.endpoint,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        **_auth_headers(cfg.api_key, cfg.headers),
                        "Content-Type": "application/json",
                    },
                    timeout=max(self.cfg.timeout, cfg.timeout),
                )
            data = json.loads(raw.decode("utf-8", "replace"))
            found = _extract_images(data)
            if not found:
                raise EndpointError(
                    "chat endpoint returned no image data: "
                    + short(raw.decode("utf-8", "replace"), 500)
                )
            target = _indexed_path(output, len(saved), count)
            target.parent.mkdir(parents=True, exist_ok=True)
            kind, value = found[0]
            if kind == "b64":
                target.write_bytes(base64.b64decode(value))
            else:
                target.write_bytes(_download(value))
            saved.append(target)
        return saved

    # -- helpers ---------------------------------------------------------
    def _save_images(self, raw: bytes, output: Path, count: int, backend: str) -> list[Path]:
        text = raw.decode("utf-8", "replace")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            if _looks_like_cloudflare(None, text):
                raise _cloudflare_error(self.cfg.edit_endpoint, text) from exc
            raise EndpointError(f"image endpoint returned non-JSON: {short(text, 400)}") from exc
        items = data.get("data") or []
        saved: list[Path] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("b64_json"):
                payload = base64.b64decode(item["b64_json"])
            elif item.get("url"):
                payload = _download(item["url"])
            else:
                continue
            target = _indexed_path(output, len(saved), max(count, len(items)))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            saved.append(target)
        if not saved:
            raise EndpointError(
                f"image endpoint returned no usable image data: {short(json.dumps(data), 500)}"
            )
        return saved


def _indexed_path(output: Path, index: int, count: int) -> Path:
    if count <= 1:
        return output
    suffix = output.suffix or ".png"
    return output.with_name(f"{output.stem}_{index + 1:02d}{suffix}")


def _download(url: str) -> bytes:
    # No Authorization header here: signed image URLs live on CDNs, not the API.
    raw, _ = _request_bytes(
        url, method="GET", headers=browser_headers(), retries=3, timeout=300.0
    )
    return raw


def data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def _extract_images(obj: Any) -> list[tuple[str, str]]:
    """Find images inside an arbitrary chat response."""
    found: list[tuple[str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("images", "image_url", "b64_json", "output_image"):
                value = node.get(key)
                if isinstance(value, str):
                    if value.startswith("data:image"):
                        try:
                            found.append(("b64", value.split(",", 1)[1]))
                        except IndexError:
                            pass
                    elif key in {"b64_json"}:
                        found.append(("b64", value))
                    elif key == "image_url" and value.startswith("http"):
                        found.append(("url", value))
                elif isinstance(value, dict):
                    walk(value)
                elif isinstance(value, list):
                    walk(value)
            url = node.get("url")
            if isinstance(url, str) and url.startswith("data:image"):
                found.append(("b64", url.split(",", 1)[1]))
            elif isinstance(url, str) and url.startswith("http"):
                imageish = node.get("type") in {"image_url", "image", "output_image"}
                imageish = imageish or bool(
                    re.search(r"\.(png|jpe?g|webp|gif|bmp)(\?|$)", url, re.I)
                )
                resultish = node.get("b64_json") is not None or node.get("revised_prompt")
                if imageish or resultish:
                    found.append(("url", url))
            for key in ("image", "content", "message", "data", "choices", "inline_data"):
                if key in node:
                    walk(node[key])
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            for match in re.finditer(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", node):
                found.append(("url", match.group(1)))
            for match in re.finditer(r"data:image/[a-zA-Z0-9.+-]+;base64,([A-Za-z0-9+/=]+)", node):
                found.append(("b64", match.group(1)))

    walk(obj)
    # de-duplicate while preserving order
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for kind, value in found:
        key = hashlib.sha1(value.encode("utf-8", "replace")).hexdigest()
        if key not in seen:
            seen.add(key)
            unique.append((kind, value))
    return unique


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


@dataclass
class Task:
    id: str
    path: Path
    title: str
    body: str
    expects: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def log_path_name(self) -> str:
        return f"{self.id}.log"

    def prompt_block(self) -> str:
        return self.body.strip()

    @classmethod
    def from_file(cls, path: Path) -> "Task":
        text = path.read_text(encoding="utf-8", errors="replace")
        meta: dict[str, Any] = {}
        body = text
        match = FRONT_MATTER.match(text)
        if match:
            body = text[match.end():]
            current_list: str | None = None
            for raw_line in match.group(1).splitlines():
                line = raw_line.rstrip()
                if not line.strip() or line.strip().startswith("#"):
                    continue
                if line.lstrip().startswith("- ") and current_list:
                    meta.setdefault(current_list, []).append(line.lstrip()[2:].strip().strip("\"'"))
                    continue
                if ":" in line:
                    key, value = line.split(":", 1)
                    key = key.strip().lower()
                    value = value.strip().strip("\"'")
                    if value:
                        meta[key] = value
                        current_list = None
                    else:
                        meta[key] = []
                        current_list = key
        task_id = str(meta.get("id") or path.stem).strip().lower()
        task_id = re.sub(r"[^a-z0-9_-]+", "-", task_id)

        expects: list[str] = []
        raw_expects = meta.get("expects") or meta.get("deliverables") or []
        if isinstance(raw_expects, str):
            expects = [p.strip() for p in raw_expects.split(",") if p.strip()]
        elif isinstance(raw_expects, list):
            expects = [str(p).strip() for p in raw_expects if str(p).strip()]

        title = str(meta.get("title") or "").strip()
        if not title:
            heading = re.search(r"^#\s+(.+)$", body, re.M)
            title = heading.group(1).strip() if heading else path.stem
        return cls(id=task_id, path=path, title=title, body=body, expects=expects, meta=meta)


def discover_tasks(cfg: Config) -> list[Task]:
    if not cfg.tasks_dir.is_dir():
        return []
    files = [
        p
        for p in cfg.tasks_dir.glob("*.md")
        if p.is_file() and not p.name.startswith((".", "_"))
    ]
    files.sort(key=natural_key)
    tasks: list[Task] = []
    seen: dict[str, Path] = {}
    for path in files:
        task = Task.from_file(path)
        if task.id in seen:
            raise SystemExit(
                red(f"duplicate task id '{task.id}': {seen[task.id].name} and {path.name}")
            )
        seen[task.id] = path
        tasks.append(task)
    return tasks


def list_ref_files(cfg: Config) -> list[Path]:
    if not cfg.ref_dir.is_dir():
        return []
    return sorted(
        (p for p in cfg.ref_dir.rglob("*") if p.is_file() and not p.name.startswith(".")),
        key=lambda p: str(p).lower(),
    )


# ---------------------------------------------------------------------------
# Sandbox + filesystem helpers
# ---------------------------------------------------------------------------


class ToolError(RuntimeError):
    pass


class Sandbox:
    """Keeps every agent file operation inside the project root."""

    def __init__(self, root: Path, writable_roots: Sequence[Path]):
        self.root = root.resolve()
        self.writable = [p.resolve() for p in writable_roots]

    def resolve(self, raw: str | Path, *, write: bool = False) -> Path:
        if raw is None or str(raw).strip() == "":
            raise ToolError("path is required")
        candidate = Path(str(raw)).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ToolError(f"path escapes the project root: {raw}") from exc
        if write:
            allowed = any(_is_within(resolved, w) for w in self.writable)
            if not allowed:
                raise ToolError(
                    "writing is only allowed inside "
                    + ", ".join(str(w.relative_to(self.root)) for w in self.writable)
                    + f" (got {resolved.relative_to(self.root)})"
                )
        return resolved

    def rel(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root))
        except ValueError:
            return str(path)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_dir": {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and folders at a path, relative to the project root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path, e.g. 'ref' or 'work'. Default '.'."},
                    "recursive": {"type": "boolean", "description": "Recurse into subdirectories (max depth 4)."},
                },
                "required": [],
            },
        },
    },
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file (code, markdown, csv, json...). Images must use read_image.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_chars": {"type": "integer", "description": "Truncate after this many characters (default 20000)."},
                },
                "required": ["path"],
            },
        },
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create or overwrite a text file inside work/ (or logs/). Use this for "
                "code, markdown, json and other text deliverables."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "e.g. 'work/report.md'"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    "read_image": {
        "type": "function",
        "function": {
            "name": "read_image",
            "description": (
                "Attach an image from ref/ (or work/) to the conversation so you can see it. "
                "Use this to study master/reference images before generating variations."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    "generate_image": {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "Generate image(s) from a prompt, optionally using one or more master "
                "reference images (image-to-image). Saved into work/."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed image prompt."},
                    "output": {
                        "type": "string",
                        "description": "Destination path in work/, e.g. 'work/images/variant.png'. With count>1, suffixes _01, _02... are added.",
                    },
                    "references": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Master/reference image paths, e.g. ['ref/master.png'].",
                    },
                    "size": {"type": "string", "description": "e.g. '1024x1024'."},
                    "quality": {"type": "string", "description": "'low', 'medium' or 'high'."},
                    "count": {"type": "integer", "description": "How many variants to generate (1-8)."},
                },
                "required": ["prompt", "output"],
            },
        },
    },
    "run_python": {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Execute a Python snippet with cwd=work/. Stdout/stderr are returned.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
    },
    "run_shell": {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command with cwd=work/. Use for tests, builds and file inspection.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    "write_log": {
        "type": "function",
        "function": {
            "name": "write_log",
            "description": (
                "Write the detailed task log to the task's log file in logs/. "
                "REQUIRED before finish_task. Include what you did, inputs used, "
                "decisions, outputs with paths, and any problems."
            ),
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
            },
        },
    },
    "append_log": {
        "type": "function",
        "function": {
            "name": "append_log",
            "description": "Append a section to the task's log file in logs/.",
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
            },
        },
    },
    "finish_task": {
        "type": "function",
        "function": {
            "name": "finish_task",
            "description": (
                "Declare the task FINALISED. Only call after the log is written and all "
                "deliverables are saved in work/."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Short report for the manager: what was produced, file paths."},
                    "status": {"type": "string", "enum": ["done", "blocked"], "description": "Use 'blocked' only if the task cannot be completed."},
                },
                "required": ["summary"],
            },
        },
    },
}

MANAGER_FINISH_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "finish_task",
        "description": (
            "Close this attempt with your verification verdict: pass (log and "
            "deliverables verified), retry (send the worker back with notes), "
            "skip (task impossible/obsolete) or halt (stop the whole workflow)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["pass", "retry", "skip", "halt"]},
                "notes": {
                    "type": "string",
                    "description": "Short, specific reason — file paths and the exact gap for retry.",
                },
            },
            "required": ["verdict", "notes"],
        },
    },
}

MANAGER_TOOL_NAMES = ("list_dir", "read_file", "read_image", "send_task_to_worker", "finish_task")

MANAGER_TOOLS: list[dict[str, Any]] = [
    TOOL_SCHEMAS["list_dir"],
    TOOL_SCHEMAS["read_file"],
    TOOL_SCHEMAS["read_image"],
    {
        "type": "function",
        "function": {
            "name": "send_task_to_worker",
            "description": (
                "Hand the current task to the WORKER agent. Blocks until the worker "
                "finalises (or gives up) and returns its report plus a fresh listing of "
                "logs/ and work/."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instructions": {
                        "type": "string",
                        "description": "Extra focus/constraints for this attempt (the full task text is passed automatically).",
                    }
                },
                "required": [],
            },
        },
    },
    MANAGER_FINISH_SCHEMA,
]

WORKER_TOOLS: list[dict[str, Any]] = [
    TOOL_SCHEMAS[name]
    for name in (
        "list_dir",
        "read_file",
        "write_file",
        "read_image",
        "generate_image",
        "run_python",
        "run_shell",
        "write_log",
        "append_log",
        "finish_task",
    )
]


def tool_call_message(name: str, arguments: dict[str, Any], call_id: str | None = None) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id or "call_" + uuid.uuid4().hex[:12],
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def normalise_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Return [{id, name, arguments(dict)}] for an assistant message."""
    out: list[dict[str, Any]] = []
    for idx, call in enumerate(message.get("tool_calls") or []):
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        name = fn.get("name") or call.get("name") or ""
        if not name:
            continue
        out.append(
            {
                "id": call.get("id") or f"call_{uuid.uuid4().hex[:10]}_{idx}",
                "name": str(name),
                "arguments": _parse_arguments(fn.get("arguments", call.get("arguments"))),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Worker agent
# ---------------------------------------------------------------------------


@dataclass
class WorkerResult:
    finalised: bool
    status: str
    summary: str
    rounds: int
    error: str = ""
    tool_events: list[str] = field(default_factory=list)


class WorkerAgent:
    """Executes one task with tools until it calls ``finish_task``."""

    def __init__(
        self,
        cfg: Config,
        client: ChatClient,
        images: ImageService,
        sandbox: Sandbox,
        task: Task,
        reporter: Reporter,
        system_prompt: str,
    ):
        self.cfg = cfg
        self.client = client
        self.images = images
        self.sandbox = sandbox
        self.task = task
        self.reporter = reporter
        self.system_prompt = system_prompt
        self.log_path = cfg.logs_dir / task.log_path_name
        self.history: list[dict[str, Any]] = []

    # -- prompt ----------------------------------------------------------
    def build_system_prompt(self) -> str:
        refs = list_ref_files(self.cfg)
        ref_list = (
            "\n".join(f"  - {self.sandbox.rel(p)} ({human_bytes(p.stat().st_size)})" for p in refs)
            or "  (ref/ is empty)"
        )
        return (
            f"{self.system_prompt.strip()}\n\n"
            "## Runtime facts\n"
            f"- Project root: {self.cfg.root}\n"
            f"- Task id: {self.task.id}\n"
            f"- Task file: {self.sandbox.rel(self.task.path)}\n"
            f"- Log file you MUST write: {self.sandbox.rel(self.log_path)}\n"
            f"- Deliverables directory: {self.sandbox.rel(self.cfg.work_dir)}\n"
            f"- Reference files currently in ref/:\n{ref_list}\n"
            f"- Shell tools: {'enabled' if self.cfg.allow_shell else 'DISABLED'}\n"
            "- All tool paths are relative to the project root.\n"
            "- Finish by calling finish_task; do not stop with prose alone.\n"
        )

    def first_user_message(self, instructions: str = "") -> str:
        refs = list_ref_files(self.cfg)
        ref_list = (
            "\n".join(f"  - {self.sandbox.rel(p)}" for p in refs) or "  (none)"
        )
        extra = f"\n\nMANAGER INSTRUCTIONS FOR THIS ATTEMPT:\n{instructions.strip()}\n" if instructions.strip() else ""
        return (
            f"TASK {self.task.id}: {self.task.title}\n"
            f"Task file: {self.sandbox.rel(self.task.path)}\n"
            f"Reference files available:\n{ref_list}\n"
            f"Log file: {self.sandbox.rel(self.log_path)}\n"
            f"Work directory: {self.sandbox.rel(self.cfg.work_dir)}\n"
            f"{extra}\n"
            "----- TASK BEGIN -----\n"
            f"{self.task.prompt_block()}\n"
            "----- TASK END -----\n\n"
            "Execute the task now. Load any reference files you need, produce the "
            "deliverables in work/, write the detailed log, then call finish_task."
        )

    # -- main loop -------------------------------------------------------
    def run(self, instructions: str = "") -> WorkerResult:
        self.history = [
            {"role": "system", "content": self.build_system_prompt()},
            {"role": "user", "content": self.first_user_message(instructions)},
        ]
        events: list[str] = []
        summary = ""
        status = "done"
        finalised = False
        nudges = 0

        for round_no in range(1, self.cfg.max_rounds_worker + 1):
            message = self.client.chat(self.history, tools=WORKER_TOOLS)
            calls = normalise_tool_calls(message)
            content = _text_of(message.get("content"))
            assistant: dict[str, Any] = {"role": "assistant", "content": content}
            if message.get("tool_calls"):
                assistant["tool_calls"] = message["tool_calls"]
            self.history.append(assistant)

            if not calls:
                self.reporter.debug(f"worker {self.task.id}: prose only, nudging")
                nudges += 1
                if nudges > 2:
                    summary = short(content, 400)
                    break
                self.history.append(
                    {
                        "role": "user",
                        "content": (
                            "Continue with tool calls. When the work is complete, write the "
                            "log and call finish_task(status='done', summary=...)."
                        ),
                    }
                )
                continue

            attachments: list[Path] = []
            finished_here = False
            for call in calls:
                name, args = call["name"], call["arguments"]
                self.reporter.event("worker", f"{self.task.id} → {name}({short(json.dumps(args), 110)})")
                events.append(name)
                try:
                    result, attach = self.dispatch(name, args)
                except ToolError as exc:
                    result, attach = f"ERROR: {exc}", []
                except EndpointError as exc:
                    result, attach = f"ERROR (endpoint): {exc}", []
                except Exception as exc:  # noqa: BLE001 - surface everything to the model
                    self.reporter.debug(traceback.format_exc())
                    result, attach = f"ERROR ({type(exc).__name__}): {exc}", []
                attachments.extend(attach)
                self.history.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": short(result, self.cfg.tool_result_max_chars)}
                )
                if name == "finish_task":
                    summary = str(args.get("summary") or "").strip() or "worker finished"
                    status = str(args.get("status") or "done")
                    finalised = True
                    finished_here = True
            if attachments:
                self._attach_images(attachments)
            if finished_here:
                break
        else:
            summary = "worker exceeded the maximum number of rounds without finishing"

        return WorkerResult(
            finalised=finalised,
            status=status,
            summary=summary,
            rounds=min(round_no, self.cfg.max_rounds_worker),
            tool_events=events,
        )

    def _attach_images(self, paths: Sequence[Path]) -> None:
        parts: list[dict[str, Any]] = [
            {"type": "text", "text": "Attached image(s) for inspection:"}
        ]
        ok = 0
        for path in paths:
            try:
                if path.stat().st_size > self.cfg.max_image_bytes:
                    parts.append({"type": "text", "text": f"(skipped large image {self.sandbox.rel(path)})"})
                    continue
                parts.append({"type": "image_url", "image_url": {"url": data_url(path)}})
                ok += 1
            except OSError as exc:
                parts.append({"type": "text", "text": f"(could not attach {path.name}: {exc})"})
        if ok:
            self.history.append({"role": "user", "content": parts})

    # -- tools -----------------------------------------------------------
    def dispatch(self, name: str, args: dict[str, Any]) -> tuple[str, list[Path]]:
        if name == "list_dir":
            return self._list_dir(args.get("path", "."), bool(args.get("recursive"))), []
        if name == "read_file":
            return self._read_file(str(args.get("path", "")), int(args.get("max_chars") or 20000)), []
        if name == "write_file":
            return self._write_file(str(args.get("path", "")), str(args.get("content") or "")), []
        if name == "read_image":
            path = self.sandbox.resolve(str(args.get("path", "")))
            if not path.is_file():
                raise ToolError(f"image not found: {args.get('path')}")
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                raise ToolError(f"not an image file: {args.get('path')}")
            return f"attached {self.sandbox.rel(path)}", [path]
        if name == "generate_image":
            return self._generate_image(args)
        if name == "run_python":
            return self._run_python(str(args.get("code") or "")), []
        if name == "run_shell":
            return self._run_shell(str(args.get("command") or "")), []
        if name == "write_log":
            content = str(args.get("content") or "")
            return self._write_log(content, append=False), []
        if name == "append_log":
            content = str(args.get("content") or "")
            return self._write_log(content, append=True), []
        raise ToolError(f"unknown tool: {name}")

    def _list_dir(self, raw: str, recursive: bool) -> str:
        base = self.sandbox.resolve(raw or ".")
        if not base.exists():
            raise ToolError(f"no such path: {raw}")
        lines: list[str] = []
        if base.is_file():
            return f"{self.sandbox.rel(base)} ({human_bytes(base.stat().st_size)})"
        for path in sorted(base.rglob("*") if recursive else base.iterdir(), key=lambda p: str(p).lower()):
            try:
                rel = path.relative_to(self.cfg.root)
            except ValueError:
                continue
            depth = len(rel.parts) - len(base.relative_to(self.cfg.root).parts)
            if recursive and depth > 4:
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            if path.is_dir():
                lines.append(f"{'  ' * (depth - 1)}{rel}/")
            else:
                lines.append(f"{'  ' * (depth - 1)}{rel} ({human_bytes(path.stat().st_size)})")
        return "\n".join(lines[:600]) or f"{raw}: empty"

    def _read_file(self, raw: str, max_chars: int) -> str:
        path = self.sandbox.resolve(raw)
        if not path.is_file():
            raise ToolError(f"no such file: {raw}")
        if path.suffix.lower() in IMAGE_SUFFIXES:
            raise ToolError("that is an image - use read_image or generate_image instead")
        max_chars = max(200, min(max_chars, 200000))
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            return text[:max_chars] + f"\n… [truncated {len(text) - max_chars} chars]"
        return text

    def _write_file(self, raw: str, content: str) -> str:
        path = self.sandbox.resolve(raw, write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"wrote {self.sandbox.rel(path)} ({len(content.encode('utf-8'))} bytes)"

    def _generate_image(self, args: dict[str, Any]) -> tuple[str, list[Path]]:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ToolError("generate_image needs a prompt")
        output = self.sandbox.resolve(str(args.get("output") or f"work/{self.task.id}.png"), write=True)
        refs = [self.sandbox.resolve(str(r)) for r in (args.get("references") or [])]
        for ref in refs:
            if not ref.is_file():
                raise ToolError(f"reference image not found: {self.sandbox.rel(ref)}")
        count = max(1, min(int(args.get("count") or 1), 8))
        saved, backend = self.images.generate(
            prompt,
            output,
            refs,
            size=args.get("size") or None,
            quality=args.get("quality") or None,
            count=count,
        )
        rels = [self.sandbox.rel(p) for p in saved]
        return (
            json.dumps({"saved": rels, "backend": backend, "count": len(rels)}),
            saved,
        )

    def _run_python(self, code: str) -> str:
        if not code.strip():
            raise ToolError("no code provided")
        scratch = self.cfg.work_dir / ".agent"
        scratch.mkdir(parents=True, exist_ok=True)
        script = scratch / f"run_{uuid.uuid4().hex[:8]}.py"
        script.write_text(code, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, script.name],
                cwd=str(self.cfg.work_dir),
                capture_output=True,
                text=True,
                timeout=self.cfg.shell_timeout,
                env={**os.environ, "PYTHONPATH": str(self.cfg.work_dir)},
            )
            return _format_proc(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired:
            raise ToolError(f"python timed out after {self.cfg.shell_timeout:.0f}s") from None

    def _run_shell(self, command: str) -> str:
        if not self.cfg.allow_shell:
            raise ToolError("shell execution is disabled (ALLOW_SHELL=false)")
        if not command.strip():
            raise ToolError("no command provided")
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(self.cfg.work_dir),
                capture_output=True,
                text=True,
                timeout=self.cfg.shell_timeout,
            )
            return _format_proc(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired:
            raise ToolError(f"command timed out after {self.cfg.shell_timeout:.0f}s") from None

    def _write_log(self, content: str, append: bool) -> str:
        if not content.strip():
            raise ToolError("empty log content")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with self.log_path.open(mode, encoding="utf-8") as fh:
            if append:
                fh.write("\n")
            fh.write(content)
            if not content.endswith("\n"):
                fh.write("\n")
        size = self.log_path.stat().st_size
        return f"{'appended to' if append else 'wrote'} {self.sandbox.rel(self.log_path)} ({size} bytes)"


def _format_proc(code: int, stdout: str, stderr: str, limit: int = 8000) -> str:
    out = (stdout or "").strip()
    err = (stderr or "").strip()
    if len(out) > limit:
        out = out[:limit] + "\n…[stdout truncated]"
    if len(err) > limit:
        err = err[:limit] + "\n…[stderr truncated]"
    return json.dumps({"exit_code": code, "stdout": out, "stderr": err})


# ---------------------------------------------------------------------------
# Manager agent
# ---------------------------------------------------------------------------


@dataclass
class ManagerVerdict:
    verdict: str  # pass | retry | skip | halt | none
    notes: str = ""


class ManagerAgent:
    """One manager session per task; it survives retries so it keeps context."""

    def __init__(
        self,
        cfg: Config,
        client: ChatClient,
        sandbox: Sandbox,
        task: Task,
        reporter: Reporter,
        system_prompt: str,
        worker_runner: Callable[[str], WorkerResult],
    ):
        self.cfg = cfg
        self.client = client
        self.sandbox = sandbox
        self.task = task
        self.reporter = reporter
        self.system_prompt = system_prompt
        self.worker_runner = worker_runner
        self.verdict = ManagerVerdict("none")
        self.history: list[dict[str, Any]] = []

    def build_system_prompt(self) -> str:
        refs = list_ref_files(self.cfg)
        ref_list = "\n".join(f"  - {self.sandbox.rel(p)}" for p in refs) or "  (none)"
        return (
            f"{self.system_prompt.strip()}\n\n"
            "## Runtime facts\n"
            f"- Project root: {self.cfg.root}\n"
            f"- Current task: {self.task.id} — {self.task.title}\n"
            f"- Task file: {self.sandbox.rel(self.task.path)}\n"
            f"- Expected log: {self.sandbox.rel(self.cfg.logs_dir / self.task.log_path_name)}\n"
            f"- Work directory: {self.sandbox.rel(self.cfg.work_dir)}\n"
            f"- Reference files:\n{ref_list}\n"
            "- Use send_task_to_worker to delegate; you cannot edit work/ yourself.\n"
            "- Objective checks are also run automatically after your verdict: the log must "
            f"exist and be at least {self.cfg.min_log_bytes} bytes"
            + (", and new/changed files must exist in work/" if self.cfg.require_work_output else "")
            + ".\n- End every attempt with finish_task and one of: pass, retry, skip, halt.\n"
        )

    def start(self, queue_context: str) -> None:
        self.history = [
            {"role": "system", "content": self.build_system_prompt()},
            {"role": "user", "content": queue_context},
        ]

    def attempt(self, attempt: int) -> ManagerVerdict:
        self.verdict = ManagerVerdict("none")
        for _ in range(1, self.cfg.max_rounds_manager + 1):
            message = self.client.chat(self.history, tools=MANAGER_TOOLS)
            calls = normalise_tool_calls(message)
            content = _text_of(message.get("content"))
            assistant: dict[str, Any] = {"role": "assistant", "content": content}
            if message.get("tool_calls"):
                assistant["tool_calls"] = message["tool_calls"]
            self.history.append(assistant)

            if not calls:
                self.history.append(
                    {
                        "role": "user",
                        "content": (
                            "Use the tools. Dispatch the task with send_task_to_worker if it "
                            "has not been done, verify the log and work/ output, then call "
                            "finish_task(verdict=...) ."
                        ),
                    }
                )
                continue

            attachments: list[Path] = []
            for call in calls:
                name, args = call["name"], call["arguments"]
                self.reporter.event("manager", f"{self.task.id} → {name}({short(json.dumps(args), 110)})")
                try:
                    result, attach = self.dispatch(name, args)
                except ToolError as exc:
                    result, attach = f"ERROR: {exc}", []
                except EndpointError as exc:
                    result, attach = f"ERROR (endpoint): {exc}", []
                except Exception as exc:  # noqa: BLE001
                    self.reporter.debug(traceback.format_exc())
                    result, attach = f"ERROR ({type(exc).__name__}): {exc}", []
                attachments.extend(attach)
                self.history.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": short(result, self.cfg.tool_result_max_chars)}
                )
            if attachments:
                parts: list[dict[str, Any]] = [{"type": "text", "text": "Attached image(s):"}]
                for path in attachments[:4]:
                    try:
                        parts.append({"type": "image_url", "image_url": {"url": data_url(path)}})
                    except OSError:
                        pass
                self.history.append({"role": "user", "content": parts})
            if self.verdict.verdict != "none":
                return self.verdict
        return self.verdict

    def feedback(self, text: str) -> None:
        self.history.append(
            {
                "role": "user",
                "content": (
                    "The previous attempt did not satisfy verification:\n"
                    f"{text.strip()}\n\n"
                    "Address the problems. Dispatch the worker again with concrete "
                    "instructions if more work is needed, verify, then call finish_task."
                ),
            }
        )

    # -- tools -----------------------------------------------------------
    def dispatch(self, name: str, args: dict[str, Any]) -> tuple[str, list[Path]]:
        if name == "list_dir":
            return self._list_dir(str(args.get("path") or "."), bool(args.get("recursive"))), []
        if name == "read_file":
            path = self.sandbox.resolve(str(args.get("path") or ""))
            if not path.is_file():
                raise ToolError(f"no such file: {args.get('path')}")
            text = path.read_text(encoding="utf-8", errors="replace")
            limit = int(args.get("max_chars") or 20000)
            return (text[:limit] + ("\n…[truncated]" if len(text) > limit else "")), []
        if name == "read_image":
            path = self.sandbox.resolve(str(args.get("path") or ""))
            if not path.is_file():
                raise ToolError(f"no such image: {args.get('path')}")
            return f"attached {self.sandbox.rel(path)}", [path]
        if name == "send_task_to_worker":
            instructions = str(args.get("instructions") or "")
            result = self.worker_runner(instructions)
            payload = {
                "worker_finalised": result.finalised,
                "worker_status": result.status,
                "worker_report": result.summary,
                "worker_rounds": result.rounds,
                "log": self._log_state(),
                "work": self._work_state(),
            }
            if result.error:
                payload["worker_error"] = result.error
            return json.dumps(payload, indent=2), []
        if name == "finish_task":
            verdict = str(args.get("verdict") or args.get("status") or "none").lower()
            if verdict in {"done", "finalised", "ok", "success", "complete"}:
                verdict = "pass"
            if verdict not in {"pass", "retry", "skip", "halt"}:
                verdict = "none"
            self.verdict = ManagerVerdict(verdict, str(args.get("notes") or args.get("summary") or ""))
            return f"verdict recorded: {self.verdict.verdict}", []
        raise ToolError(f"unknown tool: {name}")

    def _list_dir(self, raw: str, recursive: bool) -> str:
        base = self.sandbox.resolve(raw)
        if not base.exists():
            raise ToolError(f"no such path: {raw}")
        if base.is_file():
            return f"{self.sandbox.rel(base)} ({human_bytes(base.stat().st_size)})"
        lines: list[str] = []
        for path in sorted(base.rglob("*") if recursive else base.iterdir(), key=lambda p: str(p).lower()):
            if any(part.startswith(".") for part in path.relative_to(self.cfg.root).parts):
                continue
            rel = path.relative_to(self.cfg.root)
            if path.is_dir():
                lines.append(f"{rel}/")
            else:
                lines.append(f"{rel} ({human_bytes(path.stat().st_size)})")
        return "\n".join(lines[:400]) or f"{raw}: empty"

    def _log_state(self) -> str:
        path = self.cfg.logs_dir / self.task.log_path_name
        if not path.is_file():
            return f"MISSING ({self.sandbox.rel(path)})"
        return f"{self.sandbox.rel(path)} exists, {path.stat().st_size} bytes"

    def _work_state(self) -> str:
        files = sorted(p for p in self.cfg.work_dir.rglob("*") if p.is_file())
        if not files:
            return "empty"
        return "\n".join(
            f"{self.sandbox.rel(p)} ({human_bytes(p.stat().st_size)})" for p in files[:80]
        )


# ---------------------------------------------------------------------------
# Verification gate + orchestration
# ---------------------------------------------------------------------------


def snapshot_dir(path: Path) -> dict[str, tuple[int, int]]:
    state: dict[str, tuple[int, int]] = {}
    if not path.is_dir():
        return state
    for file in path.rglob("*"):
        if not file.is_file():
            continue
        try:
            stat = file.stat()
        except OSError:
            continue
        state[str(file.relative_to(path))] = (stat.st_size, stat.st_mtime_ns)
    return state


@dataclass
class GateResult:
    ok: bool
    reasons: list[str]
    changed: list[str]
    log_path: Path

    def summary(self) -> str:
        if self.ok:
            return "verified"
        return "; ".join(self.reasons)


def verify_task(cfg: Config, task: Task, before: dict[str, tuple[int, int]]) -> GateResult:
    reasons: list[str] = []
    log_path = cfg.logs_dir / task.log_path_name
    if not log_path.is_file():
        reasons.append(f"log file {cfg.logs_dir.name}/{task.log_path_name} is missing")
    else:
        size = log_path.stat().st_size
        if size < cfg.min_log_bytes:
            reasons.append(
                f"log {cfg.logs_dir.name}/{task.log_path_name} is too small "
                f"({size} bytes < {cfg.min_log_bytes})"
            )

    after = snapshot_dir(cfg.work_dir)
    changed = sorted(
        rel for rel, meta in after.items() if before.get(rel) != meta
    )

    if cfg.require_work_output and task.meta.get("work", "true") != "false":
        if task.expects:
            for pattern in task.expects:
                pattern_clean = pattern.lstrip("./")
                matches = [p for p in cfg.root.glob(pattern_clean) if p.is_file()]
                if not matches:
                    matches = [
                        p
                        for p in cfg.work_dir.rglob("*")
                        if p.is_file() and fnmatch.fnmatch(str(p.relative_to(cfg.work_dir)), pattern_clean.replace("work/", ""))
                    ]
                if not matches:
                    reasons.append(f"expected deliverable not found: {pattern}")
        elif not changed:
            reasons.append("no new or changed files in work/")

    return GateResult(not reasons, reasons, changed, log_path)


@dataclass
class TaskOutcome:
    task: Task
    status: str  # done | skipped | failed
    attempts: int
    notes: str = ""
    changed: list[str] = field(default_factory=list)


class Orchestrator:
    def __init__(self, cfg: Config, reporter: Reporter, client_factory: "ClientFactory"):
        self.cfg = cfg
        self.reporter = reporter
        self.client_factory = client_factory
        self.sandbox = Sandbox(cfg.root, [cfg.work_dir, cfg.logs_dir])
        self.results: list[TaskOutcome] = []

    # -- setup -----------------------------------------------------------
    def load_prompts(self) -> tuple[str, str]:
        manager_prompt = _read_prompt(self.cfg.manager_prompt, "manager")
        worker_prompt = _read_prompt(self.cfg.worker_prompt, "worker")
        return manager_prompt, worker_prompt

    # -- main ------------------------------------------------------------
    def run(
        self,
        tasks: list[Task],
        *,
        all_tasks: list[Task] | None = None,
    ) -> list[TaskOutcome]:
        manager_prompt, worker_prompt = self.load_prompts()
        all_tasks = all_tasks or tasks
        self.reporter.banner(
            f"Agent-Team — {len(tasks)} task(s) queued · manager={self.cfg.manager.model} "
            f"· worker={self.cfg.worker.model}"
        )
        self.reporter.info(f"run log: {self.sandbox.rel(self.reporter.path)}")

        completed: list[TaskOutcome] = []
        aborted = False
        for index, task in enumerate(tasks, start=1):
            if aborted:
                self.results.append(TaskOutcome(task, "skipped", 0, "workflow halted earlier"))
                continue
            self.reporter.banner(f"Task {index}/{len(tasks)} — {task.id}: {task.title}")
            outcome = self.run_task(task, all_tasks, completed, manager_prompt, worker_prompt)
            self.results.append(outcome)
            completed.append(outcome)

            if outcome.status == "failed":
                if not self.cfg.continue_on_failure:
                    self.reporter.error(
                        f"task {task.id} failed verification — halting workflow "
                        "(set CONTINUE_ON_FAILURE=true to keep going)"
                    )
                    aborted = True
                else:
                    self.reporter.warn(f"task {task.id} failed — continuing as configured")
            elif outcome.status == "skipped":
                self.reporter.warn(f"task {task.id} skipped by manager")

        self._print_summary()
        return self.results

    def run_task(
        self,
        task: Task,
        all_tasks: list[Task],
        completed: list[TaskOutcome],
        manager_prompt: str,
        worker_prompt: str,
    ) -> TaskOutcome:
        before = snapshot_dir(self.cfg.work_dir)
        attempts = 0
        notes = ""
        changed: list[str] = []

        manager_client = self.client_factory("manager", task)
        worker_client = self.client_factory("worker", task)
        images = self.client_factory.images()

        def worker_runner(instructions: str) -> WorkerResult:
            agent = WorkerAgent(
                self.cfg,
                worker_client,
                images,
                self.sandbox,
                task,
                self.reporter,
                worker_prompt,
            )
            return agent.run(instructions)

        manager = ManagerAgent(
            self.cfg,
            manager_client,
            self.sandbox,
            task,
            self.reporter,
            manager_prompt,
            worker_runner,
        )
        manager.start(self._queue_context(task, all_tasks, completed))

        while attempts < self.cfg.max_task_attempts:
            attempts += 1
            self.reporter.info(f"attempt {attempts}/{self.cfg.max_task_attempts} for {task.id}")
            verdict = manager.attempt(attempts)
            gate = verify_task(self.cfg, task, before)
            changed = gate.changed

            if verdict.verdict == "pass" and gate.ok:
                self.reporter.ok(f"{task.id} verified — log and work/ output confirmed")
                return TaskOutcome(task, "done", attempts, verdict.notes, changed)

            if verdict.verdict == "skip":
                self.reporter.warn(f"manager skipped {task.id}: {short(verdict.notes)}")
                return TaskOutcome(task, "skipped", attempts, verdict.notes, changed)

            if verdict.verdict == "halt":
                self.reporter.error(f"manager halted workflow on {task.id}: {short(verdict.notes)}")
                return TaskOutcome(task, "failed", attempts, verdict.notes, changed)

            problems: list[str] = []
            if verdict.verdict == "none":
                problems.append("manager did not return a verdict this attempt")
            if not gate.ok:
                problems.extend(gate.reasons)
            if verdict.notes and verdict.verdict == "retry":
                problems.append(f"manager notes: {verdict.notes}")
            reason_text = "\n- " + "\n- ".join(problems or ["verification failed"])
            self.reporter.warn(f"{task.id} attempt {attempts} not verified:{reason_text}")
            if attempts >= self.cfg.max_task_attempts:
                notes = reason_text.strip()
                break
            manager.feedback(reason_text)

        self.reporter.error(f"{task.id} exhausted {attempts} attempt(s)")
        return TaskOutcome(task, "failed", attempts, notes, changed)

    def _queue_context(self, task: Task, all_tasks: list[Task], completed: list[TaskOutcome]) -> str:
        ids = [t.id for t in all_tasks]
        pos = ids.index(task.id) + 1 if task.id in ids else 0
        queue = "\n".join(
            f"  {i + 1:02d}. {t.id} — {t.title}" for i, t in enumerate(all_tasks)
        )
        done = "\n".join(
            f"  - {o.task.id}: {o.status} ({o.attempts} attempt(s))" for o in completed
        ) or "  (none yet)"
        remaining = ", ".join(ids[pos:]) or "(none)"
        return (
            "WORKFLOW STATE\n"
            f"Position: task {pos} of {len(all_tasks)}\n"
            f"Task queue:\n{queue}\n"
            f"Completed so far:\n{done}\n"
            f"Remaining after this: {remaining}\n\n"
            f"YOUR JOB NOW: dispatch task {task.id} to the worker, wait for its "
            "FINALISED reply, verify the task log and work/ output, then call "
            "finish_task with a verdict.\n\n"
            f"Task file: {self.sandbox.rel(task.path)}\n"
            "----- TASK BEGIN -----\n"
            f"{task.prompt_block()}\n"
            "----- TASK END -----"
        )

    def _print_summary(self) -> None:
        self.reporter.banner("Workflow summary")
        for outcome in self.results:
            icon = {"done": green("✓"), "skipped": yellow("»"), "failed": red("✗")}[outcome.status]
            self.reporter.raw(
                f"  {icon} {outcome.task.id.ljust(6)} {outcome.status.ljust(8)} "
                f"{outcome.attempts} attempt(s)  {short(outcome.notes, 90)}"
            )
            for rel in outcome.changed[:6]:
                self.reporter.raw(dim(f"      work/{rel}"))
        done = sum(1 for r in self.results if r.status == "done")
        self.reporter.raw("")
        self.reporter.raw(
            f"  {done}/{len(self.results)} task(s) verified · "
            f"logs → {self.cfg.logs_dir.name}/ · work → {self.cfg.work_dir.name}/"
        )
        self.reporter.raw(f"  run log → {self.sandbox.rel(self.reporter.path)}")


def _read_prompt(path: Path, role: str) -> str:
    if not path.is_file():
        raise SystemExit(
            red(f"missing {role} prompt: {path}\nCreate {path.name} (see the shipped example).")
        )
    return path.read_text(encoding="utf-8", errors="replace").strip()


# ---------------------------------------------------------------------------
# Clients + simulation
# ---------------------------------------------------------------------------


class ClientFactory:
    """Builds chat clients (or deterministic offline stand-ins)."""

    def __init__(self, cfg: Config, reporter: Reporter):
        self.cfg = cfg
        self.reporter = reporter
        self.simulate = cfg.simulate
        self.worker_clients: list[ChatClient] = []

    def __call__(self, role: str, task: Task) -> Any:
        if self.simulate:
            return SimulatedLLM(role, task, self.cfg, self.reporter)
        if role == "manager":
            return ChatClient(self.cfg.manager, self.reporter)
        client = ChatClient(self.cfg.worker, self.reporter)
        self.worker_clients.append(client)
        return client

    def images(self) -> ImageService:
        return ImageService(self.cfg.image, self.cfg.worker, self.reporter)


class SimulatedLLM:
    """Offline deterministic stand-in so the whole pipeline can be dry-run."""

    def __init__(self, role: str, task: Task, cfg: Config, reporter: Reporter):
        self.role = role
        self.task = task
        self.task_id = task.id
        self.cfg = cfg
        self.reporter = reporter

    def chat(self, messages: list[dict[str, Any]], tools: Any = None, **_: Any) -> dict[str, Any]:
        assistant_turns = sum(1 for m in messages if m.get("role") == "assistant")
        if self.role == "manager":
            if assistant_turns % 2 == 0:
                return tool_call_message(
                    "send_task_to_worker",
                    {"instructions": "Execute the task as described and log everything."},
                )
            return tool_call_message(
                "finish_task",
                {"verdict": "pass", "notes": "simulated verification passed"},
            )

        if assistant_turns == 0:
            body = (
                f"# Task {self.task_id} — simulated execution log\n\n"
                f"- Started: {now_stamp()}\n"
                f"- Task title: {self.task.title}\n"
                f"- Agent: simulated worker (no network calls)\n"
                "- Steps:\n"
                "  1. Loaded the task definition from tasks/.\n"
                "  2. Inspected the available reference files in ref/.\n"
                "  3. Produced the deliverable listed below.\n"
                f"- Deliverables: {', '.join(path for path, _ in self._simulated_outputs())}\n"
                "- Result: success (simulation mode)\n"
            )
            return tool_call_message("write_log", {"content": body})
        if assistant_turns == 1:
            outputs = self._simulated_outputs()
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_" + uuid.uuid4().hex[:12],
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": path, "content": content}),
                        },
                    }
                    for path, content in outputs
                ],
            }
        return tool_call_message(
            "finish_task",
            {"summary": f"simulated completion of {self.task_id}", "status": "done"},
        )

    def _simulated_outputs(self) -> list[tuple[str, str]]:
        """Write the task's declared deliverables so the gate can be exercised."""
        note = (
            f"# {self.task_id} — simulated deliverable\n\n"
            "Produced by `agent_team.py run --simulate`: the offline stand-in\n"
            "writes placeholder files where the real worker would put artwork\n"
            "or code. Run again without --simulate (and with .env configured)\n"
            "for real output.\n"
        )
        outputs: list[tuple[str, str]] = []
        for pattern in self.task.expects:
            if "*" in pattern:
                target = pattern.replace("*", "simulated_01")
            else:
                target = pattern
            outputs.append((target, note))
        if not outputs:
            outputs.append((f"work/{self.task_id}_simulated.md", note))
        return outputs


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def cmd_run(cfg: Config, reporter: Reporter, args: argparse.Namespace) -> int:
    cfg.simulate = cfg.simulate or args.simulate
    if cfg.simulate:
        reporter.warn("SIMULATION MODE — no network calls, mock worker output only")

    cfg.ensure_dirs()
    tasks = discover_tasks(cfg)
    if not tasks:
        reporter.error(f"no tasks found in {cfg.tasks_dir} (expected p01.md, p02.md, …)")
        return 2

    if args.tasks:
        wanted = [t.strip().lower() for t in args.tasks.split(",") if t.strip()]
        unknown = [w for w in wanted if w not in {t.id for t in tasks}]
        if unknown:
            reporter.error(f"unknown task id(s): {', '.join(unknown)}")
            return 2
        tasks = [t for t in tasks if t.id in wanted]
    if args.start:
        start = args.start.strip().lower()
        ids = [t.id for t in tasks]
        if start not in ids:
            reporter.error(f"--start task '{start}' not found")
            return 2
        tasks = tasks[ids.index(start):]
    if args.max_tasks:
        tasks = tasks[: args.max_tasks]

    if not cfg.simulate:
        problems = _validate_endpoints(cfg)
        if problems:
            for problem in problems:
                reporter.error(problem)
            reporter.info("fix .env, or run with --simulate to test the pipeline offline")
            return 2
        missing = [p.name for p in (cfg.manager_prompt, cfg.worker_prompt) if not p.is_file()]
        if missing:
            reporter.error(f"missing prompt file(s): {', '.join(missing)}")
            return 2
        if not cfg.image.model and cfg.image.mode != "chat":
            reporter.warn(
                "IMAGE_MODEL is not set — text/code tasks will run fine, image tasks "
                "will fail until you configure it (or use IMAGE_MODE=chat)"
            )

    orchestrator = Orchestrator(cfg, reporter, ClientFactory(cfg, reporter))
    try:
        outcomes = orchestrator.run(tasks, all_tasks=discover_tasks(cfg))
    except KeyboardInterrupt:
        reporter.warn("interrupted by user")
        return 130
    except EndpointError as exc:
        reporter.error(f"endpoint failure: {exc}")
        return 1

    failed = [o for o in outcomes if o.status == "failed"]
    if failed:
        reporter.error(
            "unfinished tasks: " + ", ".join(f"{o.task.id} ({o.notes[:80]})" for o in failed)
        )
        return 1
    return 0


def _validate_endpoints(cfg: Config) -> list[str]:
    problems: list[str] = []
    for label, agent in (("MANAGER", cfg.manager), ("WORKER", cfg.worker)):
        if not agent.base_url:
            problems.append(f"{label}_BASE_URL is not set")
        elif not re.match(r"^https?://", agent.base_url):
            problems.append(f"{label}_BASE_URL must start with http:// or https:// (got {agent.base_url})")
        if not agent.model:
            problems.append(f"{label}_MODEL is not set")
    return problems


def cmd_doctor(cfg: Config, reporter: Reporter, args: argparse.Namespace) -> int:
    reporter.banner(f"Agent-Team doctor · v{__version__}")
    cfg.ensure_dirs()
    reporter.raw(cfg.describe())

    issues = 0
    reporter.raw("")
    for label, path in (
        ("tasks", cfg.tasks_dir),
        ("ref", cfg.ref_dir),
        ("logs", cfg.logs_dir),
        ("work", cfg.work_dir),
    ):
        exists = path.is_dir()
        count = len(list(path.glob("*"))) if exists else 0
        marker = green("✓") if exists else red("✗")
        reporter.raw(f"  {marker} {label.ljust(8)} {path}  ({count} entries)")
        issues += 0 if exists else 1

    for path in (cfg.manager_prompt, cfg.worker_prompt):
        exists = path.is_file()
        marker = green("✓") if exists else red("✗")
        reporter.raw(f"  {marker} prompt   {path}  ({'found' if exists else 'MISSING'})")
        issues += 0 if exists else 1

    tasks = discover_tasks(cfg)
    reporter.raw("")
    reporter.raw(f"  tasks discovered: {len(tasks)}")
    for task in tasks:
        log = cfg.logs_dir / task.log_path_name
        state = f"log {human_bytes(log.stat().st_size)}" if log.is_file() else "no log yet"
        reporter.raw(f"    · {task.id.ljust(6)} {short(task.title, 52).ljust(52)} {dim(state)}")

    problems = _validate_endpoints(cfg)
    reporter.raw("")
    if problems:
        for problem in problems:
            reporter.raw(f"  {red('✗')} {problem}")
        issues += len(problems)
    else:
        reporter.raw(f"  {green('✓')} endpoint configuration present")

    for label, agent in (("manager", cfg.manager), ("worker", cfg.worker)):
        if not agent.api_key and not agent.is_local():
            reporter.raw(f"  {yellow('!')} {label} API key is empty (ok for local servers)")
        if agent.base_url:
            reporter.raw(f"    {label}: {agent.model} → {agent.endpoint}  key={agent.masked()}")
    backend = http_backend()
    impersonation = env_str("HTTP_IMPERSONATE", "chrome") or "chrome"
    reporter.raw(
        f"    client: {backend}"
        + (f" (impersonating {impersonation})" if backend == "curl_cffi" else "")
        + f" · UA {short(browser_headers().get('User-Agent', ''), 60)}"
    )
    if backend == "urllib":
        reporter.raw(
            f"  {yellow('!')} stdlib transport — if Cloudflare answers 403/1010, "
            "`pip install curl_cffi` for a browser TLS fingerprint"
        )

    if cfg.image.model or cfg.image.mode == "chat":
        reporter.raw(
            f"    images: {cfg.image.model or '(chat fallback)'} → "
            f"{cfg.image.generate_endpoint}  mode={cfg.image.mode}"
        )
    else:
        reporter.raw(
            f"  {yellow('!')} IMAGE_MODEL is not set — image tasks cannot run "
            "(text/code tasks are unaffected)"
        )

    if args.ping and not cfg.simulate:
        reporter.raw("")
        reporter.raw("  pinging endpoints …")
        for label, agent in (("manager", cfg.manager), ("worker", cfg.worker)):
            if not agent.base_url:
                continue
            try:
                body, _ = _request_bytes(
                    agent.base_url.rstrip("/") + "/models",
                    method="GET",
                    headers=_auth_headers(agent.api_key, agent.headers),
                    timeout=30,
                    retries=1,
                )
                data = json.loads(body.decode("utf-8", "replace"))
                ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
                has_model = agent.model in ids
                reporter.raw(
                    f"    {green('✓')} {label}: {len(ids)} model(s) visible"
                    + ("" if has_model else f" {yellow('(configured model not listed)')}")
                )
            except EndpointError as exc:
                reporter.raw(f"    {red('✗')} {label}: {exc}")
                issues += 1
            except Exception as exc:  # noqa: BLE001
                reporter.raw(f"    {red('✗')} {label}: {exc}")
                issues += 1

    reporter.raw("")
    if issues:
        reporter.raw(yellow(f"doctor found {issues} issue(s)"))
        return 1
    reporter.raw(green("all good — ready to run"))
    return 0


def cmd_status(cfg: Config, reporter: Reporter, args: argparse.Namespace) -> int:
    tasks = discover_tasks(cfg)
    if not tasks:
        reporter.warn(f"no tasks in {cfg.tasks_dir}")
        return 1
    reporter.banner("Agent-Team status")
    for task in tasks:
        log = cfg.logs_dir / task.log_path_name
        log_state = f"{human_bytes(log.stat().st_size):>8}" if log.is_file() else dim("   no log")
        reporter.raw(f"  {task.id.ljust(6)} {bold(short(task.title, 50))}")
        reporter.raw(f"         log: {log_state}   expects: {', '.join(task.expects) or '(any work output)'}")
    work_files = sorted(p for p in cfg.work_dir.rglob("*") if p.is_file()) if cfg.work_dir.is_dir() else []
    reporter.raw("")
    reporter.raw(f"  work/ contains {len(work_files)} file(s)")
    for path in work_files[:25]:
        reporter.raw(dim(f"    {path.relative_to(cfg.root)} ({human_bytes(path.stat().st_size)})"))
    if len(work_files) > 25:
        reporter.raw(dim(f"    … and {len(work_files) - 25} more"))
    return 0


def cmd_new_task(cfg: Config, reporter: Reporter, args: argparse.Namespace) -> int:
    cfg.ensure_dirs()
    existing = discover_tasks(cfg)
    nxt = 1
    if existing:
        numbers = [int(m.group(1)) for t in existing if (m := re.match(r"p(\d+)$", t.id))]
        nxt = (max(numbers) if numbers else len(existing)) + 1
    task_id = args.id or f"p{nxt:02d}"
    task_id = re.sub(r"[^a-z0-9_-]+", "-", task_id.lower())
    path = cfg.tasks_dir / f"{task_id.upper()}.md"
    if path.exists():
        reporter.error(f"{path.name} already exists")
        return 1
    title = args.title or f"Task {task_id.upper()}"
    body = f"""---
id: {task_id}
title: {title}
# Optional: files that must exist after the task (relative to the project root).
# expects:
#   - work/{task_id}_output.md
---

# {title}

Describe the task for the worker agent.

## Inputs
- `ref/` — reference files are listed here (images, code, documents).

## Deliverables
- Save all output files under `work/`.
- Write a detailed log to `logs/{task_id}.log`.

## Notes
- Add any constraints, style guidance or acceptance criteria here.
"""
    path.write_text(body, encoding="utf-8")
    reporter.ok(f"created {path.relative_to(cfg.root)}")
    return 0


def cmd_init(cfg: Config, reporter: Reporter, args: argparse.Namespace) -> int:
    cfg.ensure_dirs()
    created: list[str] = []
    sample_env = cfg.root / "example.env"
    env_file = cfg.root / ".env"
    if sample_env.is_file() and not env_file.is_file():
        shutil.copyfile(sample_env, env_file)
        created.append(".env (copied from example.env — add your keys)")
    for folder in (cfg.logs_dir, cfg.work_dir, cfg.ref_dir):
        keep = folder / ".gitkeep"
        if not keep.exists() and not any(folder.iterdir()):
            keep.write_text("", encoding="utf-8")
    if not discover_tasks(cfg):
        reporter.info("no tasks found yet — add tasks/p01.md, p02.md, …")
    for line in created:
        reporter.ok(line)
    reporter.ok(f"directories ready under {cfg.root}")
    reporter.info("next: edit .env, then run `python agent_team.py doctor`")
    return 0


def cmd_selftest(cfg: Config, reporter: Reporter, args: argparse.Namespace) -> int:
    """Offline end-to-end test of the manager→worker→verify pipeline."""
    import tempfile

    reporter.banner("Agent-Team selftest (offline simulation)")
    with tempfile.TemporaryDirectory(prefix="agent-team-selftest-") as tmp:
        root = Path(tmp)
        (root / "tasks").mkdir()
        (root / "ref").mkdir()
        (root / "logs").mkdir()
        (root / "work").mkdir()
        (root / "ref" / "note.txt").write_text("reference material\n", encoding="utf-8")
        for name, title in (("p01", "First simulated task"), ("p02", "Second simulated task")):
            (root / "tasks" / f"{name}.md").write_text(
                f"---\nid: {name}\ntitle: {title}\n---\n\n# {title}\n\n"
                "Produce a short markdown note in work/ and log the process.\n",
                encoding="utf-8",
            )
        for prompt_name in ("manager.md", "worker.md"):
            src = cfg.root / prompt_name
            if src.is_file():
                shutil.copyfile(src, root / prompt_name)
            else:
                (root / prompt_name).write_text(f"# {prompt_name}\nSimulated.\n", encoding="utf-8")

        test_cfg = Config.load(root)
        test_cfg.simulate = True
        test_cfg.quiet = True
        test_cfg.root = root
        test_cfg.tasks_dir = root / "tasks"
        test_cfg.ref_dir = root / "ref"
        test_cfg.logs_dir = root / "logs"
        test_cfg.work_dir = root / "work"
        test_cfg.manager_prompt = root / "manager.md"
        test_cfg.worker_prompt = root / "worker.md"
        test_reporter = Reporter(root / "logs", quiet=True)
        test_reporter.quiet = True
        orchestrator = Orchestrator(test_cfg, test_reporter, ClientFactory(test_cfg, test_reporter))
        outcomes = orchestrator.run(discover_tasks(test_cfg))

        failures: list[str] = []
        for outcome in outcomes:
            if outcome.status != "done":
                failures.append(f"{outcome.task.id} status={outcome.status} notes={outcome.notes}")
            log = root / "logs" / f"{outcome.task.id}.log"
            if not log.is_file() or log.stat().st_size < test_cfg.min_log_bytes:
                failures.append(f"{outcome.task.id} log missing or too small")
            work = root / "work" / f"{outcome.task.id}_simulated.md"
            if not work.is_file():
                failures.append(f"{outcome.task.id} work output missing")
        if failures:
            for failure in failures:
                reporter.error(failure)
            return 1
    reporter.ok(f"selftest passed — {len(outcomes)} task(s) ran through the full pipeline")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent_team.py",
        description="Two-agent (manager + worker) workflow automation over OpenAI-compatible endpoints.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python agent_team.py doctor\n"
            "  python agent_team.py run\n"
            "  python agent_team.py run --tasks p01,p02\n"
            "  python agent_team.py run --simulate\n"
            "  python agent_team.py new-task --title 'Upscale hero image'\n"
            "  python agent_team.py selftest\n"
        ),
    )
    parser.add_argument("--root", help="project root (default: directory of this script)")
    parser.add_argument("--verbose", action="store_true", help="verbose event logging")
    parser.add_argument("--quiet", action="store_true", help="suppress console chatter")
    parser.add_argument("--version", action="version", version=f"agent-team {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="execute the task queue")
    run.add_argument("--tasks", help="comma separated task ids (default: all)")
    run.add_argument("--start", help="start from this task id")
    run.add_argument("--max-tasks", type=int, help="stop after N tasks")
    run.add_argument("--simulate", "--dry-run", dest="simulate", action="store_true",
                     help="offline simulation, no network calls")
    run.add_argument("--attempts", type=int, help="override MAX_TASK_ATTEMPTS")
    run.set_defaults(func=cmd_run)

    doctor = sub.add_parser("doctor", help="validate configuration and endpoints")
    doctor.add_argument("--ping", action="store_true", help="also call /v1/models on each endpoint")
    doctor.set_defaults(func=cmd_doctor)

    status = sub.add_parser("status", help="show tasks, logs and work/ contents")
    status.set_defaults(func=cmd_status)

    new_task = sub.add_parser("new-task", help="scaffold the next task file")
    new_task.add_argument("--id", help="task id (default: next pNN)")
    new_task.add_argument("--title", help="human readable title")
    new_task.set_defaults(func=cmd_new_task)

    init = sub.add_parser("init", help="create folders and a .env from example.env")
    init.set_defaults(func=cmd_init)

    selftest = sub.add_parser("selftest", help="offline end-to-end test of the pipeline")
    selftest.set_defaults(func=cmd_selftest)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser() if args.root else Path(__file__).resolve().parent
    cfg = Config.load(root)
    cfg.verbose = bool(args.verbose)
    cfg.quiet = bool(args.quiet)
    if getattr(args, "attempts", None):
        cfg.max_task_attempts = max(1, args.attempts)

    try:
        cfg.ensure_dirs()
    except OSError as exc:
        print(red(f"cannot create project directories under {root}: {exc}"))
        return 2

    reporter = Reporter(cfg.logs_dir, verbose=cfg.verbose, quiet=cfg.quiet)
    try:
        return int(args.func(cfg, reporter, args))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        reporter.warn("interrupted")
        return 130
    except EndpointError as exc:
        reporter.error(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
