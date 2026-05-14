"""EmcpRuntime — direct Streamable HTTP MCP client for emcp.oracle.com servers.

Used by the laptop-mode Confluence/Jira adapters as a *faster* and *more
reliable* path than `codex_proxy` (which spawns a codex LLM session and
loops in tool-calls — observed 180 s timeouts in BUG-queue-d3ec0). The
direct path measures ~10 s per page in practice.

How it works
------------

Codex's `mcp login central_confluence` stores the OAuth bundle in the
macOS Keychain under service "Codex MCP Credentials". We read that bundle
via the `security` CLI and POST JSON-RPC directly to the registered URL
using the bearer token. Codex itself silently refreshes the token in the
background; on a 401 we re-read the keychain entry to pick up the latest.

Wire format
-----------

The emcp servers speak MCP's Streamable HTTP transport: every request is
a POST with `Content-Type: application/json`, and the response is either
a single `application/json` body or an SSE stream of `event: message /
data: {...}` lines. We parse both.

Concurrency
-----------

Per-request stateless — the server does not appear to issue
`Mcp-Session-Id`. Multiple concurrent calls to the same runtime instance
are safe; we use a small lock only around `_seq` to avoid duplicate ids.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)


class EmcpError(RuntimeError):
    """Any failure talking to the emcp.oracle.com MCP server."""


class EmcpAuthError(EmcpError):
    """Specifically a 401/expired-token error — caller should refresh once."""


class EmcpRuntime:
    """Direct Streamable HTTP MCP client backed by codex's keychain credential.

    server_name: the codex-side MCP server label (e.g. 'central_confluence').
                 Used to look up the credential bundle in macOS Keychain.
    keychain_account_suffix: the hex hash that codex appends after '|' in
                 the keychain account name. Discoverable via
                 `security dump-keychain | grep 'Codex MCP Credentials'`.
    timeout_s:   per-request HTTP timeout (default 60 s — individual tool
                 calls can take 10-30 s on a cold cache).
    """

    PROTOCOL_VERSION = "2025-06-18"

    def __init__(
        self,
        server_name: str,
        keychain_account_suffix: str | None = None,
        *,
        timeout_s: float = 60.0,
        client_name: str = "kbf-emcp-direct",
        client_version: str = "1.0.0",
    ) -> None:
        """server_name: codex MCP server label (e.g. 'central_confluence').
        keychain_account_suffix: hex hash codex appends to the account name.
            If omitted, we auto-discover it from `security dump-keychain`.
        """
        self.server_name = server_name
        if not keychain_account_suffix:
            keychain_account_suffix = self._discover_keychain_account_suffix(server_name)
        self._keychain_account = f"{server_name}|{keychain_account_suffix}"
        self.timeout_s = timeout_s
        self._client_name = client_name
        self._client_version = client_version
        self._seq = 1
        self._lock = threading.Lock()
        self._cached_token: str | None = None
        self._cached_url: str | None = None
        self._cached_expires_at_ms: int | None = None
        self._initialized = False

    # ------------------------------------------------------------------
    # Credential management
    # ------------------------------------------------------------------

    @staticmethod
    def _discover_keychain_account_suffix(server_name: str) -> str:
        """Scan `security dump-keychain` for the account that matches the
        given codex MCP server. Codex stores accounts as
        '<server_name>|<16-hex-binding>'. Returns the hex suffix.

        Raises EmcpAuthError if no matching account is found.
        """
        import re
        try:
            dump = subprocess.check_output(
                ["security", "dump-keychain"],
                text=True, stderr=subprocess.PIPE, timeout=15,
            )
        except subprocess.CalledProcessError as exc:
            raise EmcpAuthError(f"security dump-keychain failed: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise EmcpAuthError("security dump-keychain timed out after 15s") from exc

        pattern = re.compile(
            rf'"acct"<blob>="{re.escape(server_name)}\|([0-9a-fA-F]+)"'
        )
        m = pattern.search(dump)
        if not m:
            raise EmcpAuthError(
                f"no Keychain entry found for codex MCP server {server_name!r}. "
                f"Run `codex mcp login {server_name}` to (re-)establish the OAuth binding."
            )
        return m.group(1)

    def _load_credential(self) -> tuple[str, str, int]:
        """Read the credential bundle from macOS Keychain.

        Returns (url, access_token, expires_at_ms). Raises EmcpAuthError if
        the keychain item is missing or unparseable.
        """
        try:
            raw = subprocess.check_output(
                ["security", "find-generic-password",
                 "-s", "Codex MCP Credentials",
                 "-a", self._keychain_account, "-w"],
                text=True, stderr=subprocess.PIPE,
            ).strip()
        except subprocess.CalledProcessError as exc:
            raise EmcpAuthError(
                f"keychain read failed for {self._keychain_account!r}: "
                f"{exc.stderr.strip() if exc.stderr else exc}. "
                f"Run `codex mcp login {self.server_name}` to (re-)establish the OAuth."
            ) from exc
        try:
            bundle = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EmcpAuthError(
                f"keychain entry for {self._keychain_account!r} is not JSON: {exc}"
            ) from exc
        url = bundle.get("url")
        token = (bundle.get("token_response") or {}).get("access_token")
        expires_at = bundle.get("expires_at")
        if not url or not token:
            raise EmcpAuthError(
                f"keychain entry for {self._keychain_account!r} missing url or access_token"
            )
        return url, token, int(expires_at or 0)

    def _ensure_token(self, force_refresh: bool = False) -> tuple[str, str]:
        """Return (url, access_token), refreshing from keychain when needed."""
        # Refresh if forced, no cached token, or token expires within 60 s.
        now_ms = int(time.time() * 1000)
        if (
            force_refresh
            or self._cached_token is None
            or self._cached_url is None
            or (self._cached_expires_at_ms or 0) <= now_ms + 60_000
        ):
            url, token, expires_at = self._load_credential()
            self._cached_url = url
            self._cached_token = token
            self._cached_expires_at_ms = expires_at
            log.info(
                "emcp[%s]: credential refreshed (expires in %.0f min)",
                self.server_name,
                max(0, (expires_at - now_ms) / 60_000),
            )
        return self._cached_url, self._cached_token  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # JSON-RPC framing over Streamable HTTP
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        with self._lock:
            n = self._seq
            self._seq += 1
            return n

    @staticmethod
    def _parse_sse_or_json(body: str) -> dict:
        """Parse either an SSE-framed response (event: message\\ndata: {...})
        or a plain JSON body. Returns the first JSON object found."""
        # SSE framing: walk lines, return first `data: ...` payload.
        for line in body.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        # Plain JSON fallback.
        return json.loads(body)

    def _post(self, body: dict, *, retried: bool = False) -> tuple[int, str]:
        """Single POST; raises EmcpAuthError on 401, EmcpError on other errors.

        On 401 the caller can retry once after force-refreshing the token.
        """
        url, token = self._ensure_token()
        req = urllib.request.Request(
            url, method="POST",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": self.PROTOCOL_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and not retried:
                # Token might have just expired between our check and the
                # request. Force-refresh from keychain (codex rotates them
                # in the background) and try once more.
                log.warning("emcp[%s]: 401 — refreshing token and retrying", self.server_name)
                self._ensure_token(force_refresh=True)
                return self._post(body, retried=True)
            raise EmcpError(f"HTTP {exc.code} from {self.server_name}: {exc.read().decode(errors='replace')[:200]}") from exc
        except urllib.error.URLError as exc:
            raise EmcpError(f"network error talking to {self.server_name}: {exc}") from exc

    def _ensure_initialized(self) -> None:
        """Run the MCP initialize handshake once per process lifetime."""
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            init_body = {
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {
                    "protocolVersion": self.PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": self._client_name,
                        "version": self._client_version,
                    },
                },
            }
            status, body = self._post(init_body)
            resp = self._parse_sse_or_json(body)
            if "error" in resp:
                raise EmcpError(f"emcp initialize failed: {resp['error']}")
            log.info(
                "emcp[%s]: initialized (server=%s status=%d)",
                self.server_name,
                (resp.get("result", {}).get("serverInfo") or {}).get("name", "?"),
                status,
            )
            # Send the initialized notification — no response expected (202).
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._initialized = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_tools(self) -> list[dict]:
        """Return the server's tool catalog (one dict per tool, with name + inputSchema)."""
        self._ensure_initialized()
        status, body = self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list",
        })
        resp = self._parse_sse_or_json(body)
        if "error" in resp:
            raise EmcpError(f"tools/list failed: {resp['error']}")
        return resp.get("result", {}).get("tools", []) or []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict:
        """Call an MCP tool; return the `result` dict (with `content` blocks)."""
        self._ensure_initialized()
        body = {
            "jsonrpc": "2.0", "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        t0 = time.time()
        status, raw = self._post(body)
        resp = self._parse_sse_or_json(raw)
        elapsed = time.time() - t0
        if "error" in resp:
            raise EmcpError(
                f"tools/call {name} failed in {elapsed:.1f}s: {resp['error']}"
            )
        log.info(
            "emcp[%s]: tools/call %s in %.1fs",
            self.server_name, name, elapsed,
        )
        return resp.get("result", {}) or {}

    def call_tool_for_text(self, name: str, arguments: dict[str, Any]) -> str:
        """Convenience: call a tool and extract the first text content block.

        Most emcp tools return their JSON payload inside a single
        `content[0].text` block. This unwraps that one layer.
        """
        result = self.call_tool(name, arguments)
        for block in result.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
        raise EmcpError(f"tool {name} returned no text content: {result!r}")

    def close(self) -> None:
        # Stateless HTTP — no socket to close. Defined for API parity.
        self._initialized = False
