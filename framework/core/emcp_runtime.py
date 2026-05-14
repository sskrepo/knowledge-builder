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
import urllib.parse
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
        # Serialise refresh-token grant — this server rotates refresh_token
        # and invalidates the old one on use. Concurrent refreshes would burn
        # the credential. Refresh from a single thread at a time.
        self._refresh_lock = threading.Lock()
        self._cached_token: str | None = None
        self._cached_url: str | None = None
        self._cached_expires_at_ms: int | None = None
        self._token_endpoint_cache: str | None = None
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

    def _load_credential_bundle(self) -> dict:
        """Read the full credential bundle from macOS Keychain.

        Bundle shape (codex's format):
          {
            "server_name": "...",
            "url": "...",
            "client_id": "...",
            "token_response": {
              "access_token": "...", "refresh_token": "...",
              "token_type": "Bearer", "expires_in": 3600, "scope": "..."
            },
            "expires_at": <unix-ms>
          }
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
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EmcpAuthError(
                f"keychain entry for {self._keychain_account!r} is not JSON: {exc}"
            ) from exc

    def _write_credential_bundle(self, bundle: dict) -> None:
        """Persist updated credential bundle back to macOS Keychain.

        Uses `security add-generic-password -U` (update if exists). Required
        after a refresh-token grant because this server rotates the
        refresh_token — codex needs the new bundle for its own subsequent
        calls or both we and codex will fail with 400 on the next refresh.
        """
        proc = subprocess.run(
            ["security", "add-generic-password", "-U",
             "-s", "Codex MCP Credentials",
             "-a", self._keychain_account,
             "-w", json.dumps(bundle)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise EmcpAuthError(
                f"keychain write failed for {self._keychain_account!r}: "
                f"{proc.stderr.strip()[:200]}. "
                f"This breaks subsequent refreshes — the refresh_token grant "
                f"already consumed the old refresh_token. Run "
                f"`codex mcp login {self.server_name}` to recover."
            )

    def _load_credential(self) -> tuple[str, str, int]:
        """Legacy 3-tuple shape: (url, access_token, expires_at_ms)."""
        bundle = self._load_credential_bundle()
        url = bundle.get("url")
        token = (bundle.get("token_response") or {}).get("access_token")
        expires_at = bundle.get("expires_at")
        if not url or not token:
            raise EmcpAuthError(
                f"keychain entry for {self._keychain_account!r} missing url or access_token"
            )
        return url, token, int(expires_at or 0)

    # ------------------------------------------------------------------
    # OAuth refresh-token flow
    # ------------------------------------------------------------------

    def _discover_token_endpoint(self) -> str:
        """Discover the OAuth token endpoint via the RFC 9728 metadata chain:

            POST <resource_url> → 401 with WWW-Authenticate carrying
                                  resource_metadata=<url>
            GET  /.well-known/oauth-protected-resource/<path>
                → authorization_servers[0]
            GET  /.well-known/oauth-authorization-server/<auth_path>
                → token_endpoint

        We skip the WWW-Authenticate step and use the resource URL we
        already have. Cached after first successful discovery.
        """
        if self._token_endpoint_cache:
            return self._token_endpoint_cache

        # url comes from the keychain bundle — has the resource path baked in.
        url, _, _ = self._load_credential()
        parsed = urllib.parse.urlparse(url)
        prm_url = (
            f"{parsed.scheme}://{parsed.netloc}"
            f"/.well-known/oauth-protected-resource{parsed.path}"
        )
        with urllib.request.urlopen(prm_url, timeout=self.timeout_s) as r:
            prm = json.loads(r.read().decode("utf-8"))
        auth_servers = prm.get("authorization_servers") or []
        if not auth_servers:
            raise EmcpAuthError(
                f"{prm_url} returned no authorization_servers — cannot refresh"
            )
        as_url = auth_servers[0]
        au = urllib.parse.urlparse(as_url)
        asm_url = (
            f"{au.scheme}://{au.netloc}"
            f"/.well-known/oauth-authorization-server{au.path}"
        )
        with urllib.request.urlopen(asm_url, timeout=self.timeout_s) as r:
            asm = json.loads(r.read().decode("utf-8"))
        token_endpoint = asm.get("token_endpoint")
        if not token_endpoint:
            raise EmcpAuthError(
                f"{asm_url} returned no token_endpoint — cannot refresh"
            )
        if "refresh_token" not in (asm.get("grant_types_supported") or []):
            raise EmcpAuthError(
                f"{asm_url} does not advertise refresh_token grant — "
                f"manual `codex mcp login {self.server_name}` required"
            )
        self._token_endpoint_cache = token_endpoint
        log.info(
            "emcp[%s]: discovered token_endpoint=%s",
            self.server_name, token_endpoint,
        )
        return token_endpoint

    def _refresh_access_token(self) -> None:
        """Run the OAuth refresh_token grant against the IdP, persist new
        bundle to keychain, and update the in-memory cache.

        Serialised by _refresh_lock — this server rotates refresh tokens
        and invalidates the old one on use. Concurrent refreshes would
        burn the credential. Inside the lock we also re-check the keychain
        because another thread may have already refreshed while we waited.
        """
        with self._refresh_lock:
            # Double-check: another thread may have refreshed while we waited.
            bundle = self._load_credential_bundle()
            now_ms = int(time.time() * 1000)
            if int(bundle.get("expires_at") or 0) > now_ms + 60_000:
                log.info(
                    "emcp[%s]: another thread already refreshed; adopting new bundle",
                    self.server_name,
                )
                self._cached_url = bundle["url"]
                self._cached_token = bundle["token_response"]["access_token"]
                self._cached_expires_at_ms = int(bundle["expires_at"])
                return

            refresh_token = (bundle.get("token_response") or {}).get("refresh_token")
            client_id = bundle.get("client_id")
            if not refresh_token or not client_id:
                raise EmcpAuthError(
                    f"keychain bundle for {self.server_name} missing "
                    f"refresh_token or client_id — cannot refresh"
                )

            token_endpoint = self._discover_token_endpoint()
            body = urllib.parse.urlencode({
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            }).encode("utf-8")
            req = urllib.request.Request(
                token_endpoint, method="POST", data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                    new_tokens = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                raise EmcpAuthError(
                    f"refresh_token grant rejected by {token_endpoint} "
                    f"(HTTP {exc.code}): {detail}. The refresh_token may have "
                    f"already been consumed or expired — run "
                    f"`codex mcp login {self.server_name}` to recover."
                ) from exc

            expires_in_s = int(new_tokens.get("expires_in") or 3600)
            new_bundle = {
                **bundle,
                "token_response": new_tokens,
                "expires_at": int((time.time() + expires_in_s) * 1000),
            }

            # CRITICAL: persist back to keychain BEFORE updating in-memory
            # cache. If write fails we don't want anyone to think we have a
            # working token — the old refresh_token has been consumed and
            # is no longer valid for either us or codex.
            self._write_credential_bundle(new_bundle)
            self._cached_url = new_bundle["url"]
            self._cached_token = new_tokens["access_token"]
            self._cached_expires_at_ms = new_bundle["expires_at"]
            log.info(
                "emcp[%s]: refreshed via OAuth refresh_token grant; "
                "keychain updated (token expires in %.0f min)",
                self.server_name, expires_in_s / 60,
            )

    def _ensure_token(self, force_refresh: bool = False) -> tuple[str, str]:
        """Return (url, access_token).

        Flow:
          - Cache miss / force_refresh → reload from keychain.
          - If keychain token also expired or within 60 s of expiring →
            run OAuth refresh_token grant (which writes back to keychain).

        The proactive expiry check is critical because the IdP rotates
        refresh tokens on every use; we want at most one refresh per hour
        per runtime, not one per request.
        """
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
                "emcp[%s]: credential loaded from keychain (expires in %.0f min)",
                self.server_name,
                max(0, (expires_at - now_ms) / 60_000),
            )
            # Keychain entry itself is expired or near-expiry → refresh via OAuth.
            if expires_at <= now_ms + 60_000:
                log.info(
                    "emcp[%s]: keychain token expired/near-expiry — "
                    "running OAuth refresh", self.server_name,
                )
                self._refresh_access_token()
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
                # Two possibilities:
                #   (a) keychain has a newer token codex rotated for us; or
                #   (b) the token is actually expired and we need to run our
                #       own refresh_token grant.
                # Try (a) first by re-reading keychain; if that token is also
                # stale (expired or matches what we just sent and failed),
                # _ensure_token's proactive check inside _refresh_access_token
                # will fire the OAuth grant.
                log.warning(
                    "emcp[%s]: 401 — re-reading keychain, then OAuth refresh",
                    self.server_name,
                )
                try:
                    # Always run the OAuth refresh on a real 401 — re-reading
                    # the keychain alone won't help if codex hasn't rotated.
                    self._refresh_access_token()
                except EmcpAuthError:
                    # Surface the auth failure, but include the original 401
                    # body for diagnostics.
                    raise
                return self._post(body, retried=True)
            raise EmcpError(
                f"HTTP {exc.code} from {self.server_name}: "
                f"{exc.read().decode(errors='replace')[:200]}"
            ) from exc
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
