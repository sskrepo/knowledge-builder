"""Shared runtime for the `codex_proxy` adapter mode (laptop only).

Spawns `codex mcp-server` as a child process, performs the MCP initialize
handshake, and exposes a single high-level method — `call_codex_tool(prompt)`
— that runs an LLM-mediated Codex session and returns its structured JSON
response.

Background: Codex CLI stores Atlassian MCP servers as HTTPS+OAuth URLs in
~/.codex/config.toml. There is no spawn-command to launch directly. The only
laptop path that reuses Codex's OAuth is to ask Codex itself to call those
MCP servers on our behalf, via `codex mcp-server`'s `codex` tool. That tool
runs a Codex LLM session, which can delegate to the configured remote MCP
servers and return a synthesized answer.

Discovery facts (2026-05-11):
- `codex mcp-server` exposes 2 tools: `codex` (run a Codex session) and
  `codex-reply` (continue an existing thread).
- Wire protocol: newline-delimited JSON-RPC 2.0 over stdio.
- Codex emits many `codex/event` notifications during a session; the
  response to `tools/call` arrives last and carries `content[0].text`
  containing the final Codex output.
- Typical latency: 30-60s per call.

This module is laptop-only — adapters that use it must enforce the
`KBF_ENV in (dev, laptop)` guard at factory time.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from queue import Empty, Queue
from typing import Any

log = logging.getLogger(__name__)

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


class CodexProxyError(RuntimeError):
    pass


class CodexProxyRuntime:
    """One subprocess per adapter instance; thread-safe `call_codex_tool`."""

    def __init__(
        self,
        codex_bin: str = "codex",
        spawn_args: list[str] | None = None,
        sandbox: str = "read-only",
        approval_policy: str = "never",
        request_timeout_s: float = 180.0,
        init_timeout_s: float = 10.0,
    ) -> None:
        self.codex_bin = codex_bin
        self.spawn_args = spawn_args or ["mcp-server"]
        self.sandbox = sandbox
        self.approval_policy = approval_policy
        self.request_timeout_s = request_timeout_s
        self.init_timeout_s = init_timeout_s
        self._proc: subprocess.Popen | None = None
        self._seq = 1
        self._lock = threading.Lock()
        self._responses: dict[int, dict] = {}
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._response_event = threading.Event()
        self._init_done = False
        self.last_thread_id: str | None = None

    # ------------------------------------------------------------------
    # Subprocess lifecycle
    # ------------------------------------------------------------------

    def _start(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            log.info("spawning codex mcp-server")
            self._proc = subprocess.Popen(
                [self.codex_bin, *self.spawn_args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            self._init_done = False
        if self._reader is None or not self._reader.is_alive():
            self._reader_stop.clear()
            self._reader = threading.Thread(target=self._reader_loop, daemon=True)
            self._reader.start()
        self._initialize()

    def _reader_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            if self._reader_stop.is_set():
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                log.debug("non-JSON from codex mcp-server: %s", text[:200])
                continue
            if "id" in obj:
                self._responses[obj["id"]] = obj
                self._response_event.set()
            else:
                log.debug("codex mcp-server notification: %s", obj.get("method"))

    def _write(self, msg: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self._proc.stdin.flush()

    def _wait_for_response(self, req_id: int, timeout_s: float) -> dict:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if req_id in self._responses:
                return self._responses.pop(req_id)
            self._response_event.wait(timeout=0.5)
            self._response_event.clear()
            if self._proc is not None and self._proc.poll() is not None:
                raise CodexProxyError(
                    f"codex mcp-server exited (code={self._proc.poll()}) "
                    f"before responding to id={req_id}"
                )
        raise CodexProxyError(
            f"codex mcp-server did not respond to id={req_id} within {timeout_s}s"
        )

    def _initialize(self) -> None:
        if self._init_done:
            return
        self._write({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "kbf-codex-proxy", "version": "1.0.0"},
            },
        })
        resp = self._wait_for_response(0, self.init_timeout_s)
        if "error" in resp:
            raise CodexProxyError(f"initialize failed: {resp['error']}")
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._init_done = True

    # ------------------------------------------------------------------
    # Tool call
    # ------------------------------------------------------------------

    def call_codex_tool(
        self,
        prompt: str,
        *,
        timeout_s: float | None = None,
        thread_id: str | None = None,
    ) -> str:
        """Invoke Codex via tools/call and return the textual content.

        Use `thread_id` (set automatically after first call) to continue a
        session via `codex-reply` — cheaper than starting a new session.
        """
        with self._lock:
            self._start()
            req_id = self._seq
            self._seq += 1
            if thread_id is not None or self.last_thread_id is not None:
                use_thread = thread_id or self.last_thread_id
                params = {
                    "name": "codex-reply",
                    "arguments": {"threadId": use_thread, "prompt": prompt},
                }
            else:
                params = {
                    "name": "codex",
                    "arguments": {
                        "prompt": prompt,
                        "approval-policy": self.approval_policy,
                        "sandbox": self.sandbox,
                    },
                }
            self._write({
                "jsonrpc": "2.0", "id": req_id,
                "method": "tools/call", "params": params,
            })

        resp = self._wait_for_response(req_id, timeout_s or self.request_timeout_s)
        if "error" in resp:
            raise CodexProxyError(f"codex tool error: {resp['error']}")
        result = resp.get("result") or {}
        structured = result.get("structuredContent") or {}
        if structured.get("threadId"):
            self.last_thread_id = structured["threadId"]
        text = structured.get("content")
        if not text:
            content_blocks = result.get("content") or []
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    break
        if not text:
            raise CodexProxyError(f"empty codex response: {result}")
        return text

    def call_for_json(
        self,
        prompt: str,
        *,
        timeout_s: float | None = None,
        thread_id: str | None = None,
    ) -> Any:
        """Run the codex tool and parse a JSON object/array from the response.

        The prompt should instruct Codex to return JSON. This helper tolerates
        markdown ```json fences and surrounding whitespace.
        """
        text = self.call_codex_tool(prompt, timeout_s=timeout_s, thread_id=thread_id)
        return parse_json_from_codex_response(text)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._proc is None:
            return
        self._reader_stop.set()
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self._proc = None
        self._init_done = False
        self.last_thread_id = None

    def __enter__(self) -> CodexProxyRuntime:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ----------------------------------------------------------------------
# JSON extraction helpers
# ----------------------------------------------------------------------

def parse_json_from_codex_response(text: str) -> Any:
    """Extract a JSON value from Codex's response text.

    Tolerates: bare JSON, ```json``` fenced blocks, ``` ``` (no language)
    fenced blocks, and surrounding prose. Raises CodexProxyError if no
    parseable JSON value is found.
    """
    text = text.strip()
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try fenced block
    m = _FENCE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError as exc:
            raise CodexProxyError(
                f"fenced block was not valid JSON: {exc}\nblock: {m.group(1)[:300]}"
            ) from exc
    # Last-ditch: find first { or [ and try to parse up to matching close
    for opener, closer in [("{", "}"), ("[", "]")]:
        start = text.find(opener)
        if start < 0:
            continue
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    raise CodexProxyError(f"no JSON found in codex response: {text[:300]}")
