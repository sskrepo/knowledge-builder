"""Confluence Codex CLI adapter — MCP over stdio subprocess transport.

Per ADR-020. Reads ~/.codex/config.toml (or cfg.config_path), finds the
[[mcpServers]] entry matching cfg.server_name, spawns it as a subprocess,
and speaks JSON-RPC over the process's stdio.

This transport is laptop-only; the factory enforces KBF_ENV in ("dev", "laptop").
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .._base import (
    ChangeEvent,
    HealthReport,
    RawItem,
    RawItemRef,
    SourceQuery,
)
from .shared import to_raw_item

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TOML parser — use stdlib tomllib (3.11+) with tomli fallback
# ---------------------------------------------------------------------------

def _load_toml(path: Path) -> dict:
    try:
        import tomllib  # type: ignore[import]
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except ModuleNotFoundError:
        try:
            import tomli  # type: ignore[import]
            with open(path, "rb") as fh:
                return tomli.load(fh)
        except ModuleNotFoundError as exc:
            raise ImportError(
                "tomllib (Python 3.11+) or tomli must be installed to read "
                "~/.codex/config.toml. Install tomli: pip install tomli"
            ) from exc


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ConfluenceCodexCliAdapter:
    """Confluence adapter that speaks MCP JSON-RPC over a stdio subprocess.

    The subprocess is the MCP server registered in ~/.codex/config.toml under
    the [[mcpServers]] entry whose name matches cfg["server_name"].  The process
    is kept alive for the lifetime of the adapter session (process-per-session).
    """

    name = "confluence:codex_cli"
    kind = "confluence"
    mode = "codex_cli"

    def __init__(self, cfg: dict) -> None:
        self.server_name: str = cfg["server_name"]
        self.config_path: Path = Path(
            os.path.expanduser(cfg.get("config_path", "~/.codex/config.toml"))
        )
        self.tool_map: dict[str, str] = cfg["tool_map"]
        self.required_caps: list[str] = cfg.get("required_capabilities", [])
        self.timeout_s: int = cfg.get("timeout_seconds", 60)
        self.max_retries: int = cfg.get("max_retries", 2)
        self.poll_interval: int = cfg.get("poll_interval_seconds", 300)
        self._proc: subprocess.Popen | None = None
        self._seq: int = 0
        self._lock = threading.Lock()  # serialise stdin/stdout access

    # ------------------------------------------------------------------
    # Subprocess lifecycle
    # ------------------------------------------------------------------

    def _load_server_entry(self) -> dict:
        """Parse config.toml and return the [[mcpServers]] block for server_name."""
        if not self.config_path.exists():
            raise FileNotFoundError(
                f"Codex config not found at {self.config_path}. "
                "Is Codex installed? Run: npm install -g @openai/codex"
            )
        cfg = _load_toml(self.config_path)
        servers: list[dict] = cfg.get("mcpServers", [])
        for entry in servers:
            if entry.get("name") == self.server_name:
                return entry
        raise KeyError(
            f"Server '{self.server_name}' not found in {self.config_path}. "
            f"Run: codex mcp add  (available: {[s.get('name') for s in servers]})"
        )

    def _spawn(self) -> None:
        """Read config.toml, find server entry, spawn subprocess, perform MCP initialize handshake."""
        if self._proc is not None and self._proc.poll() is None:
            return  # already running

        entry = self._load_server_entry()
        command: str = entry["command"]
        args: list[str] = entry.get("args", [])
        env_overrides: dict = entry.get("env", {})

        # Merge env overrides on top of current environment
        proc_env = {**os.environ, **env_overrides}

        log.info(
            "spawning MCP subprocess",
            extra={"server": self.server_name, "command": command, "args": args},
        )
        self._proc = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=proc_env,
        )
        log.debug("MCP subprocess pid=%d", self._proc.pid)

        # MCP initialize handshake (id=0 reserved for this)
        init_req = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "kb-framework", "version": "1.0.0"},
            },
        }
        self._write_message(init_req)
        init_resp = self._read_message(timeout=self.timeout_s)
        if "error" in init_resp:
            raise RuntimeError(
                f"MCP initialize failed for '{self.server_name}': {init_resp['error']}"
            )
        log.debug("MCP initialize OK, server info: %s", init_resp.get("result", {}).get("serverInfo"))

        # Send initialized notification (no id — it's a notification)
        notification = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }
        self._write_message(notification)
        # Start seq at 1; 0 was used for initialize
        self._seq = 1

    # ------------------------------------------------------------------
    # JSON-RPC framing helpers
    # ------------------------------------------------------------------

    def _write_message(self, msg: dict) -> None:
        """Write a newline-delimited JSON message to the subprocess stdin."""
        assert self._proc is not None and self._proc.stdin is not None
        line = json.dumps(msg) + "\n"
        self._proc.stdin.write(line.encode())
        self._proc.stdin.flush()

    def _read_message(self, timeout: int | None = None) -> dict:
        """Read one newline-delimited JSON message from subprocess stdout."""
        assert self._proc is not None and self._proc.stdout is not None

        # Use a thread to enforce timeout on the blocking readline()
        result: list[bytes] = []
        error: list[Exception] = []

        def _read() -> None:
            try:
                result.append(self._proc.stdout.readline())  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                error.append(exc)

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout=timeout or self.timeout_s)
        if t.is_alive():
            raise TimeoutError(
                f"No response from MCP subprocess '{self.server_name}' within {timeout or self.timeout_s}s"
            )
        if error:
            raise error[0]

        line = result[0]
        if not line:
            rc = self._proc.poll()
            raise RuntimeError(
                f"MCP subprocess '{self.server_name}' closed stdout unexpectedly (exit code: {rc})"
            )
        return json.loads(line.decode())

    # ------------------------------------------------------------------
    # RPC / tool call
    # ------------------------------------------------------------------

    def _rpc(self, method: str, params: dict) -> dict:
        """Send one JSON-RPC request, return the result dict."""
        with self._lock:
            req_id = self._seq
            self._seq += 1
            req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
            self._write_message(req)
            resp = self._read_message(timeout=self.timeout_s)

        if resp.get("id") != req_id:
            raise RuntimeError(
                f"RPC id mismatch: sent {req_id}, got {resp.get('id')}"
            )
        if "error" in resp:
            raise RuntimeError(
                f"JSON-RPC error from '{self.server_name}' method={method}: {resp['error']}"
            )
        return resp.get("result", {})

    def _call_tool(self, tool: str, args: dict) -> dict:
        """Call an MCP tool over the stdio subprocess."""
        self._ensure_proc()
        result = self._rpc("tools/call", {"name": tool, "arguments": args})
        if "error" in result:
            raise RuntimeError(f"MCP tool error from {tool}: {result['error']}")
        return result.get("content", result)

    def _ensure_proc(self) -> None:
        """Spawn subprocess if it hasn't been started or has crashed."""
        if self._proc is None or self._proc.poll() is not None:
            self._spawn()

    # ------------------------------------------------------------------
    # Adapter Protocol
    # ------------------------------------------------------------------

    def healthcheck(self) -> HealthReport:
        try:
            self._ensure_proc()
            result = self._rpc("tools/list", {})
            tools = [t["name"] for t in result.get("tools", [])]
            missing = [
                op for op in self.required_caps
                if self.tool_map.get(op) not in tools
            ]
            if missing:
                return HealthReport(
                    False, self.mode,
                    f"required capabilities missing: {missing}",
                    capabilities=tools,
                )
            return HealthReport(True, self.mode, "ok", capabilities=tools)
        except FileNotFoundError as exc:
            return HealthReport(False, self.mode, str(exc))
        except KeyError as exc:
            return HealthReport(False, self.mode, str(exc))
        except Exception as exc:  # noqa: BLE001
            return HealthReport(False, self.mode, str(exc))

    def list(self, q: SourceQuery) -> Iterable[RawItemRef]:
        if not q.space:
            raise ValueError("Confluence codex_cli list requires SourceQuery.space")
        tool = self.tool_map["list_pages_in_space"]
        cursor = None
        while True:
            args: dict = {"spaceKey": q.space, "limit": 50}
            if cursor:
                args["start"] = cursor
            if q.labels_include:
                args["labels"] = q.labels_include
            res = self._call_tool(tool, args)
            pages = res.get("results", res.get("content", []))
            if not pages:
                break
            for p in pages:
                yield RawItemRef(
                    kind="confluence_page",
                    source="confluence",
                    source_id=str(p.get("id", p.get("contentId"))),
                    last_modified=_parse_iso((p.get("version") or {}).get("when")),
                )
            cursor = res.get("nextStart")
            if not cursor:
                break

    def fetch(self, ref: RawItemRef) -> RawItem:
        tool = self.tool_map["get_page_by_id"]
        payload = self._call_tool(
            tool, {"pageId": ref.source_id, "expand": "body.storage,metadata.labels"}
        )
        return self.normalize(payload, ref.source_id)

    def stream_changes(self, since: datetime) -> Iterable[ChangeEvent]:
        if "search" not in self.tool_map:
            return iter([])
        cql = f'lastmodified >= "{since.strftime("%Y-%m-%d")}"'
        res = self._call_tool(self.tool_map["search"], {"cql": cql, "limit": 50})
        for p in res.get("results", []):
            yield ChangeEvent(
                kind="updated",
                source="confluence",
                source_id=str(p.get("id")),
                timestamp=_parse_iso((p.get("version") or {}).get("when")) or datetime.utcnow(),
            )

    def discover(self, recipe: list[dict]) -> Iterable[RawItemRef]:
        """Discover refs from a recipe list of {space, labels_include, ...} dicts."""
        for step in recipe:
            q = SourceQuery(
                space=step.get("space"),
                labels_include=step.get("labels_include", []),
                labels_exclude=step.get("labels_exclude", []),
                extra=step.get("extra", {}),
            )
            yield from self.list(q)

    def normalize(self, mcp_response: dict, source_id: str) -> RawItem:
        """Translate MCP tool output to RawItem — identical to mcp.py normalize()."""
        if "body" in mcp_response or "id" in mcp_response:
            payload = mcp_response
        else:
            content = mcp_response.get("content") or []
            if content and isinstance(content[0], dict) and "text" in content[0]:
                payload = json.loads(content[0]["text"])
            else:
                payload = mcp_response
        metadata = {
            "title": payload.get("title"),
            "space": (payload.get("space") or {}).get("key"),
            "version": (payload.get("version") or {}).get("number"),
            "updated_at": (payload.get("version") or {}).get("when"),
            "labels": [
                lbl.get("name")
                for lbl in (payload.get("metadata", {}).get("labels", {}).get("results", []))
            ],
        }
        return to_raw_item(payload=payload, metadata=metadata, source_id=str(source_id))

    def close(self) -> None:
        """Terminate the MCP subprocess and release resources."""
        if self._proc is not None:
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
            log.info("MCP subprocess '%s' terminated", self.server_name)
            self._proc = None

    def __enter__(self) -> ConfluenceCodexCliAdapter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
