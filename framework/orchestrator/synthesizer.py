"""synthesizer — produces a cited answer from a ContextPacket.

Per ADR-007 amend 2 (structured synthesis output).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from ..core.llm import LLMClient

log = logging.getLogger(__name__)


def _is_content_filter_error(exc: BaseException) -> bool:
    """Return True when *exc* originates from an upstream content-policy rejection.

    Matches OCI GenAI 400 "Inappropriate content detected!!!" and any HTTP-400
    from an inference provider whose message contains "Inappropriate content".
    """
    msg = str(exc)
    return "Inappropriate content" in msg or (
        "400" in msg and "content" in msg.lower()
    )


@dataclass
class SynthesisSection:
    name: str
    description: str
    required: bool = True
    max_chars: int | None = None


@dataclass
class SynthesisSchema:
    name: str
    sections: list[SynthesisSection] = field(default_factory=list)


# Built-in schemas
INCIDENT_RCA = SynthesisSchema(
    name="incident_rca",
    sections=[
        SynthesisSection("Root_Cause", "Likely root cause based on retrieved context."),
        SynthesisSection("Resolution", "Recommended resolution steps based on past incidents."),
        SynthesisSection("Similar_ticket_for_reference", "Most similar past ticket ID and one-line summary."),
    ],
)

GENERIC_QA = SynthesisSchema(
    name="generic_qa",
    sections=[
        SynthesisSection("Answer", "Direct answer to the user's question, citing sources inline."),
        SynthesisSection("Citations", "List of citation URLs used."),
    ],
)


class Synthesizer:
    def __init__(self, llm: LLMClient, model: str = "gpt-4o"):
        self.llm = llm
        self.model = model

    def synthesize(
        self,
        query: str,
        passages: list,        # list[Passage]
        schema: SynthesisSchema = GENERIC_QA,
        budget=None,
    ) -> dict:
        if not passages:
            return {section.name: "(no relevant context found)" for section in schema.sections}

        sections_block = "\n".join(
            f"  {s.name}: {s.description}" for s in schema.sections
        )
        required = [s.name for s in schema.sections if s.required]

        passages_block = "\n\n".join(
            f"[{i}] (citation: {p.citation.url})\n{p.text}"
            for i, p in enumerate(passages)
        )

        system = f"""You synthesize a cited answer from retrieved evidence.

Output STRICTLY in the following format (one section per line; section name
followed by colon, then content). Use [N] inline citations matching passage
numbers.

Required sections (MUST appear): {required}

Sections:
{sections_block}

Rules:
- Stay grounded in the passages; do not invent facts.
- Every claim must reference at least one [N] citation.
- If a section has no support, write "(no support in retrieved context)".
"""
        user = f"Query: {query}\n\nPassages:\n{passages_block}\n\nProduce the structured answer now."

        try:
            response = self.llm.chat(
                model=self.model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                temperature=0.0,
                max_tokens=(budget.max_tokens_out if budget else 1500),
            )
        except Exception as exc:
            if _is_content_filter_error(exc):
                request_id = f"KBF-{uuid.uuid4().hex[:12].upper()}"
                log.warning(
                    "synthesizer: content-filter rejection from inference provider "
                    "(requestId=%s): %s",
                    request_id, exc,
                )
                # Return a clean tier_4 no_answer — no provider details exposed.
                no_answer = {s.name: "(no relevant context found)" for s in schema.sections}
                no_answer["_content_filtered"] = True
                no_answer["_request_id"] = request_id
                return no_answer
            raise
        return self._parse_sections(response["text"], schema)

    def _parse_sections(self, text: str, schema: SynthesisSchema) -> dict:
        out: dict[str, str] = {s.name: "" for s in schema.sections}
        current = None
        for line in text.splitlines():
            stripped = line.strip()
            matched = False
            for s in schema.sections:
                if stripped.startswith(f"{s.name}:"):
                    current = s.name
                    out[current] = stripped[len(s.name) + 1:].strip()
                    matched = True
                    break
            if not matched and current:
                out[current] = (out[current] + "\n" + line).strip()
        return out
