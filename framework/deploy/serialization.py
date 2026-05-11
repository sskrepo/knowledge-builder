"""camelCase <-> snake_case serialization for PDD V3 external API surface.

Rules:
- DB column names: snake_case (PostgreSQL/Oracle convention) — internal only.
- Python dict keys: snake_case (Python convention) — internal only.
- External API JSON: camelCase — all REST responses and MCP tool return values.

Usage:
  # In a route handler:
  return to_camel_response({"synth_id": "...", "created_at": "..."})
  # Returns: {"synthId": "...", "createdAt": "..."}

Field name invariants (from openapi.yaml schemas):
  synth_id            -> synthId
  created_at          -> createdAt
  skill_name          -> skillName
  tier_used           -> tierUsed
  citation_url        -> citationUrl
  source_sha          -> sourceSha
  persona_allowlist   -> personaAllowlist
  token_budget_per_request -> tokenBudgetPerRequest
"""
from __future__ import annotations

import re

from fastapi.responses import JSONResponse


def snake_to_camel(name: str) -> str:
    """Convert snake_case to camelCase.

    Examples:
        synth_id      -> synthId
        created_at    -> createdAt
        skill_name    -> skillName
        tier_used     -> tierUsed
        citation_url  -> citationUrl
        source_sha    -> sourceSha
    """
    components = name.split("_")
    return components[0] + "".join(x.title() for x in components[1:])


def camel_to_snake(name: str) -> str:
    """Convert camelCase to snake_case.

    Examples:
        synthId      -> synth_id
        createdAt    -> created_at
        skillName    -> skill_name
        tierUsed     -> tier_used
        citationUrl  -> citation_url
        sourceSha    -> source_sha
    """
    # Handle sequences like "XMLParser" -> "XML_Parser" before standard split
    s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s1).lower()


def convert_keys(obj: object, converter) -> object:
    """Recursively apply key converter to all dict keys in a nested structure.

    Handles:
    - dict: convert all keys, recurse into values
    - list: recurse into each element
    - any other type: returned as-is (values are never converted, only keys)
    """
    if isinstance(obj, dict):
        return {converter(k): convert_keys(v, converter) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_keys(item, converter) for item in obj]
    return obj


def to_camel_response(data: dict, status_code: int = 200) -> JSONResponse:
    """Convert a snake_case dict to a camelCase JSON HTTP response.

    All route handlers call this before returning so the external API surface
    consistently uses camelCase field names per the OpenAPI 3.1 spec.

    Args:
        data: Snake_case dict to serialize (keys converted recursively).
        status_code: HTTP status code (default 200).

    Returns:
        FastAPI JSONResponse with Content-Type: application/json and
        all dict keys converted to camelCase.
    """
    return JSONResponse(
        status_code=status_code,
        content=convert_keys(data, snake_to_camel),
    )


def from_camel_request(body: dict) -> dict:
    """Convert a camelCase request body to snake_case for internal use.

    Called at the top of route handlers after reading request JSON so that
    all internal code deals exclusively with snake_case keys.

    Args:
        body: CamelCase dict from the parsed request body.

    Returns:
        Dict with all keys converted to snake_case (recursively).
    """
    return convert_keys(body, camel_to_snake)
