"""Versioned skill prompt served via MCP ``prompts/get``.

The prompt teaches LLM clients:
  - How to use the KBF tools (askKnowledgeBase, authorSkill)
  - The error-handling loop: read requestId → call reportBug → tell user
  - How to upload local files via uploadArtifact for artifact-driven skill authoring

MCP prompts/list returns the prompt descriptor.
MCP prompts/get returns the full messages list.
"""
from __future__ import annotations

SKILL_PROMPT_VERSION = "1.2.0"

SKILL_PROMPT_NAME = "kbf-skill-prompt"

SKILL_PROMPT_DESCRIPTION = (
    "System prompt for LLM clients interacting with the "
    "Knowledge Builder Framework MCP server"
)


def get_skill_prompt_messages() -> list[dict]:
    """Return MCP prompt messages list for the kbf-skill-prompt.

    Returns:
        A list containing one message object in MCP prompts/get format:
        ``[{"role": "user", "content": {"type": "text", "text": ...}}]``
    """
    return [
        {
            "role": "user",
            "content": {
                "type": "text",
                "text": _PROMPT_TEXT.strip(),
            },
        }
    ]


_PROMPT_TEXT = """
You are interacting with the Knowledge Builder Framework (KBF) MCP server.

## Available tools

- **askKnowledgeBase** — query the knowledge base with a natural-language question
- **authorSkill** — start or continue a knowledge-building session
  - Start: pass only `input` (e.g. "start")
  - Continue: pass both `synthId` (from previous response) and `input`
- **reportBug** — report an error you received from any KBF tool
- **uploadArtifact** — upload a local file (PPT/DOCX/MD/TXT) for analysis in an authorSkill session

## Session flow for authorSkill

1. Call `authorSkill` with `input: "start"` to begin. Save the returned `synth_id`.
2. Each subsequent turn: call `authorSkill` with `synthId: <saved_id>` and `input: <your message>`.
3. When `done: true` appears in the response, the session is complete.

## Error handling (IMPORTANT)

If any tool returns a response with `isError: true`:
1. Note the `requestId` field in the error response.
2. Immediately call `reportBug` with:
   - `requestId`: the value from the error response
   - `tool`: the name of the tool that failed
   - `description`: a brief description of what you were trying to do
   - `input`: the input you provided (optional but helpful)
3. Tell the user: "I've reported this error to the KBF team (request ID: <requestId>). They'll investigate and fix it."
4. Do NOT retry the same call with identical arguments — it will fail again.

## Using local files as example artifacts

If you have a local PPT, DOCX, Markdown, or plain-text file to use as the
example outcome during skill authoring:

1. Read the file as raw bytes and base64-encode them using your available tools.
2. Call `uploadArtifact` with:
   - `content`:  the base64-encoded bytes
   - `filename`: the original filename including extension (e.g. "q2-highlights.pptx")
   - `synthId`:  the session ID from the current authorSkill session
3. Note the `artifactId` returned in the response.
4. When the authorSkill session asks you to provide an artifact path,
   respond with: "artifact:<filename> id:<artifactId>"
   Example: "artifact:q2-highlights.pptx id:art-3f7a1b2c"

Important:
- The file must be .pptx, .docx, .md, or .txt.
- The file must be 10 MB or smaller.
- If you do not have a file, you can type field names manually instead
  (the session will prompt you for this).
- uploadArtifact requires write scope — the same token used for authorSkill.

## Prompt version

{version}
""".format(version=SKILL_PROMPT_VERSION)
