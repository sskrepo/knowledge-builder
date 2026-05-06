"""URN scheme for cross-entity references.

Per ADR-009: `urn:faaas:{kind}:{id}` (and chunk variant `...#chunk_{N}`).
"""
from __future__ import annotations

import re

URN_PREFIX = "urn:faaas"

_URN_RE = re.compile(r"^urn:faaas:(?P<kind>[a-z_]+):(?P<id>[^#]+)(#chunk_(?P<chunk>\d+))?$")


def make(kind: str, id_: str, chunk: int | None = None) -> str:
    base = f"{URN_PREFIX}:{kind}:{id_}"
    return base if chunk is None else f"{base}#chunk_{chunk}"


def parse(urn: str) -> dict:
    m = _URN_RE.match(urn)
    if not m:
        raise ValueError(f"invalid URN: {urn!r}")
    d = m.groupdict()
    return {
        "kind": d["kind"],
        "id": d["id"],
        "chunk": int(d["chunk"]) if d.get("chunk") else None,
    }


# Common builders ---------------------------------------------------------
def resource(id_: str) -> str:        return make("resource", id_)
def service(id_: str) -> str:         return make("service", id_)
def functional_area(id_: str) -> str: return make("functional_area", id_)
def persona(id_: str) -> str:         return make("persona", id_)
def content(corpus: str, source_id: str, chunk: int | None = None) -> str:
    return make("content", f"{corpus}:{source_id}", chunk)
def tenant(id_: str) -> str:          return make("tenant", id_)
