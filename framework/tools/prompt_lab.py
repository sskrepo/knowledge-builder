"""prompt_lab — ADR-030 prompt-test harness CLI.

Standalone CLI for iterating on LLM prompts in framework/config/prompts/*.yaml
without walking a full 17-state authorSkill session.

FIXTURE FORMAT
--------------
A fixture file is a JSON object with these keys::

    {
      "fixture_id":  "failure_classifier_gold",   # unique name
      "prompt_id":   "failure_classifier",         # must match a prompt in the store
      "description": "Human-readable description of this test case",
      "persona":     null,                         # or "tpm", "pm", etc.
      "vars": {
        "var_name": "var_value",                   # maps to **fmt_vars in get_prompt()
        ...
      }
    }

The "vars" dict maps directly to the **fmt_vars argument of get_prompt(). Values may
be strings, dicts, or lists — they are JSON-serialised to strings before formatting
so the template always receives strings.

"persona" (if non-null) is passed to get_prompt(persona=...) for overlay resolution.

USAGE
-----
    python -m framework.tools.prompt_lab --list
    python -m framework.tools.prompt_lab run <prompt_id> --fixture <path.json> [options]
    python -m framework.tools.prompt_lab docs [--output <path>]

OPTIONS (run subcommand)
    --fixture <path>    Path to fixture JSON file (required for live run)
    --dry-run           Format prompt and print; no LLM call
    --reload            Force registry.reload() before running (picks up YAML edits)
    --runs N            Execute the live call N times; print stability summary
    --persona <p>       Override/supply persona (used for overlay resolution)
    --expected <path>   After single live run, diff parsed output vs saved JSON; print PASS/DIFF

NO-STUB POLICY (CLAUDE.md)
---------------------------
Live runs call the real OCI GenAI LLM. If the LLM is unreachable (stub mode detected),
the harness prints BLOCKED and exits non-zero. No mock fallback. This is intentional.

To refresh your OCI token:
    oci session authenticate --profile adpcpprod --region eu-frankfurt-1
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Path setup — allow running from any directory
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_PROMPTS_DIR = _REPO_ROOT / "framework" / "config" / "prompts"
_DOCS_OUTPUT = _REPO_ROOT / "docs" / "wiki" / "authorskill-prompts.md"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("prompt_lab")

# ---------------------------------------------------------------------------
# OCI / LLM constants (mirror test_failure_classifier_gate.py)
# ---------------------------------------------------------------------------

_OCI_ENDPOINT = "https://inference.generativeai.eu-frankfurt-1.oci.oraclecloud.com"
_COMPARTMENT_OCID = "ocid1.compartment.oc1..aaaaaaaax7wbfdtfl7axhfae7q5lwvrmf2nlcdii3scarukqmuos7u5mokla"
_OCI_PROFILE = "adpcpprod"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture(path: Path) -> Dict[str, Any]:
    """Load and basic-validate a fixture JSON file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: cannot read fixture file {path}: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: fixture file {path} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    for required_key in ("fixture_id", "prompt_id", "vars"):
        if required_key not in data:
            print(
                f"ERROR: fixture {path} is missing required key '{required_key}'. "
                f"Fixture format: fixture_id, prompt_id, vars (and optionally persona, description).",
                file=sys.stderr,
            )
            sys.exit(1)
    return data


def _serialise_vars(vars_dict: Dict[str, Any]) -> Dict[str, str]:
    """Convert fixture vars to string values expected by get_prompt(**fmt_vars).

    Dicts and lists are JSON-serialised with indent=2. Strings pass through unchanged.
    This matches how call sites in conversation.py serialise structured inputs.
    """
    result: Dict[str, str] = {}
    for k, v in vars_dict.items():
        if isinstance(v, str):
            result[k] = v
        else:
            result[k] = json.dumps(v, indent=2)
    return result


def _make_llm():
    """Return a live OCI GenAI LLM client.

    Exits with code 2 if LLM is in stub mode (OCI unreachable or token expired).
    Never returns a stub — per CLAUDE.md no-stub policy.
    """
    from framework.core.llm import OciGenAiLLMClient

    llm = OciGenAiLLMClient(
        endpoint=_OCI_ENDPOINT,
        compartment_ocid=_COMPARTMENT_OCID,
        auth="config_file",
        config_profile=_OCI_PROFILE,
    )
    # Probe — a stub returns {"_stub": true}
    try:
        probe = llm.chat(
            model="synthesis",
            messages=[{"role": "user", "content": "Reply with the single word: ALIVE"}],
            max_tokens=10,
        )
        text = probe.get("text", "") if isinstance(probe, dict) else str(probe)
        if "_stub" in text:
            print(
                "BLOCKED — LLM unreachable (stub mode detected).\n"
                "Refresh token: oci session authenticate --profile adpcpprod --region eu-frankfurt-1",
                file=sys.stderr,
            )
            sys.exit(2)
    except Exception as exc:
        print(
            f"BLOCKED — LLM connectivity probe failed: {exc}\n"
            "Refresh token: oci session authenticate --profile adpcpprod --region eu-frankfurt-1",
            file=sys.stderr,
        )
        sys.exit(2)
    return llm


def _parse_json_output(raw: str) -> Optional[Dict[str, Any]]:
    """Parse LLM JSON output using the shared framework helper.

    Falls back to a simple regex-based extraction if the import fails.
    Uses framework.skill_builder.review._parse_llm_json_response so that harness
    parsing == production parsing (ADR-030 P3 requirement).
    """
    try:
        from framework.skill_builder.review import _parse_llm_json_response
        return _parse_llm_json_response(raw)
    except ImportError:
        pass
    except ValueError:
        pass

    # Minimal fallback — strip fences and try json.loads
    import re
    cleaned = re.sub(r"```(?:json)?\n?(.*?)\n?```", r"\1", raw, flags=re.S).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


def _diff_json(actual: Dict[str, Any], expected: Dict[str, Any]) -> List[str]:
    """Return a list of diff lines describing differences between actual and expected.

    Returns an empty list when they are identical.
    """
    diffs: List[str] = []
    all_keys = set(actual.keys()) | set(expected.keys())
    for key in sorted(all_keys):
        if key not in expected:
            diffs.append(f"  EXTRA key in output:   {key!r} = {actual[key]!r}")
        elif key not in actual:
            diffs.append(f"  MISSING key in output: {key!r} (expected: {expected[key]!r})")
        elif actual[key] != expected[key]:
            diffs.append(
                f"  CHANGED {key!r}:\n"
                f"    expected: {expected[key]!r}\n"
                f"    actual  : {actual[key]!r}"
            )
    return diffs


def _stability_summary(results: List[Optional[Dict[str, Any]]]) -> None:
    """Print a stability summary for N live runs of the same prompt."""
    parsed_results = [r for r in results if r is not None]
    if not parsed_results:
        print("\n=== STABILITY SUMMARY ===")
        print("No parseable JSON outputs to compare.")
        return

    all_keys: set = set()
    for r in parsed_results:
        all_keys.update(r.keys())

    print("\n=== STABILITY SUMMARY ===")
    print(f"Runs: {len(results)}  Parseable: {len(parsed_results)}")
    print(f"{'Key':<30} {'Stable?':<10} Values")
    print("-" * 80)
    for key in sorted(all_keys):
        values = [r.get(key, "<missing>") for r in parsed_results]
        unique_vals = list({json.dumps(v, sort_keys=True) for v in values})
        stable = len(unique_vals) == 1
        sample = values[0] if stable else values
        stable_str = "YES" if stable else "NO"
        val_repr = repr(sample)[:60]
        print(f"  {key:<28} {stable_str:<10} {val_repr}")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    """--list: print a table of all prompts in the store."""
    from framework.skill_builder.prompt_registry import get_registry, PromptStoreError

    try:
        reg = get_registry(_PROMPTS_DIR)
    except PromptStoreError as exc:
        print(f"ERROR: failed to load prompt store from {_PROMPTS_DIR}: {exc}", file=sys.stderr)
        return 1

    prompts = reg.list_prompts()
    if not prompts:
        print("No prompts found in store.")
        return 0

    # Table header
    col_id = 30
    col_ver = 8
    col_model = 12
    col_locked = 8
    col_desc = 50
    header = (
        f"{'PROMPT ID':<{col_id}} {'VER':<{col_ver}} {'MODEL':<{col_model}} "
        f"{'LOCKED':<{col_locked}} DESCRIPTION"
    )
    print(header)
    print("-" * (col_id + col_ver + col_model + col_locked + col_desc + 4))
    for p in prompts:
        locked_str = "LOCKED" if p.locked else ""
        desc = (p.description[:col_desc - 3] + "...") if len(p.description) > col_desc else p.description
        print(
            f"{p.prompt_id:<{col_id}} {p.version:<{col_ver}} {p.model:<{col_model}} "
            f"{locked_str:<{col_locked}} {desc}"
        )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """run subcommand: format prompt, optionally call LLM, print results."""
    from framework.skill_builder.prompt_registry import (
        get_registry,
        MissingVarsError,
        PromptNotFoundError,
        PromptStoreError,
    )

    prompt_id: str = args.prompt_id
    fixture_path: Optional[Path] = Path(args.fixture) if args.fixture else None
    dry_run: bool = args.dry_run
    do_reload: bool = args.reload
    n_runs: int = args.runs
    persona_override: Optional[str] = args.persona
    expected_path: Optional[Path] = Path(args.expected) if args.expected else None

    # Load registry
    try:
        reg = get_registry(_PROMPTS_DIR)
    except PromptStoreError as exc:
        print(f"ERROR: failed to load prompt store: {exc}", file=sys.stderr)
        return 1

    # Hot-reload if requested
    if do_reload:
        print(f"[reload] Reloading registry from {_PROMPTS_DIR} ...")
        try:
            reg.reload()
            print("[reload] Done — YAML changes are now active.")
        except PromptStoreError as exc:
            print(f"ERROR: reload failed: {exc}", file=sys.stderr)
            return 1

    # Load fixture
    fmt_vars: Dict[str, str] = {}
    persona: Optional[str] = persona_override
    if fixture_path:
        fixture = _load_fixture(fixture_path)
        # Fixture prompt_id must match CLI prompt_id
        fixture_pid = fixture.get("prompt_id")
        if fixture_pid and fixture_pid != prompt_id:
            print(
                f"WARNING: fixture prompt_id={fixture_pid!r} != CLI prompt_id={prompt_id!r}. "
                f"Proceeding with CLI prompt_id.",
                file=sys.stderr,
            )
        raw_vars = fixture.get("vars", {})
        fmt_vars = _serialise_vars(raw_vars)
        # persona from fixture, unless overridden on CLI
        if persona is None:
            persona = fixture.get("persona") or None

    # Format the prompt (resolves vars + overlays)
    try:
        spec = reg.get_prompt(prompt_id, persona=persona, **fmt_vars)
    except PromptNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except MissingVarsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            f"Tip: add the missing variable(s) to your fixture's 'vars' dict "
            f"at {fixture_path or '<no fixture>'}",
            file=sys.stderr,
        )
        return 1
    except PromptStoreError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Print prompt metadata
    print(f"\n=== PROMPT: {spec.prompt_id} (v{spec.version}) ===")
    print(f"Model:          {spec.model}")
    print(f"Max tokens:     {spec.max_tokens}")
    print(f"Response format:{spec.response_format}")
    if persona:
        print(f"Persona:        {persona}")

    # Print formatted prompt (truncated unless --show-full-prompt)
    show_full = getattr(args, "show_full_prompt", False)
    prompt_text = spec.text
    if show_full:
        print(f"\n--- Formatted prompt (full) ---\n{prompt_text}\n")
    else:
        truncated = prompt_text[:500]
        suffix = " ...[truncated — use --show-full-prompt to see all]" if len(prompt_text) > 500 else ""
        print(f"\n--- Formatted prompt (first 500 chars) ---\n{truncated}{suffix}\n")

    if dry_run:
        print("[dry-run] No LLM call made.")
        return 0

    if spec.model == "none":
        print(
            f"[info] Prompt '{prompt_id}' has model=none (turn message template, not an LLM call). "
            f"No LLM call made. Use --dry-run to inspect the formatted text.",
            file=sys.stderr,
        )
        return 0

    # Live LLM run(s)
    llm = _make_llm()
    all_parsed: List[Optional[Dict[str, Any]]] = []

    for run_i in range(1, n_runs + 1):
        if n_runs > 1:
            print(f"\n--- Run {run_i}/{n_runs} ---")
        t0 = time.time()
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Calling LLM ...")

        result = llm.chat(
            model=spec.model,
            messages=[{"role": "user", "content": spec.text}],
            response_format=spec.response_format if spec.response_format else None,
            max_tokens=spec.max_tokens,
        )
        elapsed = time.time() - t0
        raw_text = result.get("text", "") if isinstance(result, dict) else str(result)
        print(f"Elapsed: {elapsed:.1f}s")
        print(f"\n--- Raw LLM output ---\n{raw_text}\n")

        parsed: Optional[Dict[str, Any]] = None
        if spec.response_format and spec.response_format.get("type") == "json_object":
            parsed = _parse_json_output(raw_text)
            if parsed is not None:
                print("--- Parsed JSON ---")
                print(json.dumps(parsed, indent=2))
            else:
                print("WARNING: could not parse LLM output as JSON.", file=sys.stderr)
        all_parsed.append(parsed)

        if n_runs > 1 and run_i < n_runs:
            time.sleep(1)

    # Stability summary for multi-run
    if n_runs > 1:
        _stability_summary(all_parsed)

    # --expected diff (single run only)
    if expected_path and n_runs == 1:
        first_parsed = all_parsed[0]
        try:
            expected_data = json.loads(expected_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: cannot load expected file {expected_path}: {exc}", file=sys.stderr)
            return 1

        if first_parsed is None:
            print("\nDIFF: cannot compare — LLM output was not parseable JSON.")
        else:
            diffs = _diff_json(first_parsed, expected_data)
            if not diffs:
                print(f"\nPASS — output matches expected file {expected_path}")
            else:
                print(f"\nDIFF — output differs from expected file {expected_path}:")
                for line in diffs:
                    print(line)

    return 0


def cmd_docs(args: argparse.Namespace) -> int:
    """docs subcommand: generate authorskill-prompts.md from YAML store."""
    from framework.skill_builder.prompt_registry import (
        PromptRegistry,
        PromptStoreError,
        _parse_overlays,
    )

    output_path = Path(args.output) if args.output else _DOCS_OUTPUT

    # Load registry
    try:
        reg = PromptRegistry(_PROMPTS_DIR)
    except PromptStoreError as exc:
        print(f"ERROR: failed to load prompt store: {exc}", file=sys.stderr)
        return 1

    # Load overlays YAML for the per-prompt overlay listing
    persona_overlays_path = _PROMPTS_DIR / "persona_overlays.yaml"
    persona_overlay_map: Dict[str, Any] = {}
    if persona_overlays_path.exists():
        import yaml
        try:
            raw_ov = yaml.safe_load(persona_overlays_path.read_text(encoding="utf-8"))
            persona_overlay_map = _parse_overlays(raw_ov, str(persona_overlays_path))
        except Exception as exc:
            log.warning("Could not parse persona_overlays.yaml for docs: %s", exc)

    # Load raw YAML data for full template text + required_vars + notes
    import yaml as _yaml
    raw_prompts: Dict[str, Dict[str, Any]] = {}  # prompt_id -> raw yaml stanza
    for yaml_file in sorted(_PROMPTS_DIR.glob("*.yaml")):
        if yaml_file.name == "persona_overlays.yaml":
            continue
        try:
            data = _yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if data and isinstance(data.get("prompts"), dict):
                for pid, stanza in data["prompts"].items():
                    if isinstance(stanza, dict):
                        raw_prompts[pid] = stanza
        except Exception as exc:
            log.warning("Skipping %s in docs generation: %s", yaml_file, exc)

    now_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    lines: List[str] = []
    lines.append("---")
    lines.append("title: authorSkill — Full Prompt Dump (GENERATED — DO NOT HAND-EDIT)")
    lines.append("source: framework/config/prompts/*.yaml")
    lines.append("generator: python -m framework.tools.prompt_lab docs")
    lines.append(f"generated_at: {now_ts}")
    lines.append("owner: architect")
    lines.append("tags: [skill-builder, prompts, adr-030]")
    lines.append("status: generated")
    lines.append("---")
    lines.append("")
    lines.append("# authorSkill — Full Prompt Dump")
    lines.append("")
    lines.append("> **GENERATED — DO NOT HAND-EDIT.**")
    lines.append("> Source: `framework/config/prompts/*.yaml`")
    lines.append(f"> Generated at: `{now_ts}`")
    lines.append("> Regenerate with: `python -m framework.tools.prompt_lab docs`")
    lines.append("")

    # List all prompts sorted by id
    all_meta = reg.list_prompts()
    lines.append("## Prompt Index")
    lines.append("")
    for meta in all_meta:
        locked_badge = " **[LOCKED]**" if meta.locked else ""
        lines.append(f"- [{meta.prompt_id}](#{meta.prompt_id}){locked_badge} — {meta.description}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for meta in all_meta:
        pid = meta.prompt_id
        stanza = raw_prompts.get(pid, {})
        required_vars = stanza.get("required_vars") or []
        notes = stanza.get("notes") or ""
        template = stanza.get("template") or reg._raw_template(pid)
        locked = meta.locked
        model = meta.model
        max_tokens = stanza.get("max_tokens", 0)
        response_format = stanza.get("response_format", "")
        version = meta.version
        checksum = stanza.get("checksum", "")

        lines.append(f"## {pid}")
        lines.append("")
        if notes:
            lines.append(f"> {notes}")
            lines.append("")

        # Metadata table
        lines.append("| Field | Value |")
        lines.append("|---|---|")
        lines.append(f"| id | `{pid}` |")
        lines.append(f"| version | `{version}` |")
        lines.append(f"| model | `{model}` |")
        lines.append(f"| max_tokens | `{max_tokens}` |")
        lines.append(f"| response_format | `{response_format}` |")
        lines.append(f"| locked | `{locked}` |")
        if checksum:
            lines.append(f"| checksum | `{checksum}` |")
        lines.append(f"| required_vars | `{', '.join(required_vars)}` |")
        lines.append("")

        # Persona overlays for this prompt
        applies_to_personas = [
            p for p, info in sorted(persona_overlay_map.items())
            if pid in info.get("applies_to", [])
        ]
        if applies_to_personas:
            lines.append(f"**Persona overlays:** {', '.join(applies_to_personas)}")
            lines.append("")

        # Template body
        lines.append("**Template:**")
        lines.append("")
        lines.append("```")
        lines.append(template.rstrip("\n"))
        lines.append("```")
        lines.append("")
        lines.append("---")
        lines.append("")

    # Persona overlays section
    if persona_overlay_map:
        lines.append("## Persona Overlays")
        lines.append("")
        lines.append("Source: `framework/config/prompts/persona_overlays.yaml`")
        lines.append("")
        for persona_name, info in sorted(persona_overlay_map.items()):
            lines.append(f"### {persona_name}")
            lines.append("")
            lines.append(f"**Applies to:** {', '.join(info.get('applies_to', []))}")
            lines.append("")
            overlay_vars = info.get("overlay_vars", {})
            for var_name, var_val in sorted(overlay_vars.items()):
                lines.append(f"**`{var_name}`:**")
                lines.append("")
                lines.append("```")
                lines.append(str(var_val).rstrip("\n"))
                lines.append("```")
                lines.append("")
            lines.append("---")
            lines.append("")

    content = "\n".join(lines) + "\n"

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    print(f"Generated: {output_path}")
    print(f"Prompts: {len(all_meta)}")
    print(f"Personas: {len(persona_overlay_map)}")
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prompt_lab",
        description="ADR-030 prompt-test harness — iterate on prompts without a full authorSkill session.",
    )

    # Top-level --list
    parser.add_argument(
        "--list",
        action="store_true",
        default=False,
        help="List all prompts in the store (id, version, model, locked, description).",
    )

    subparsers = parser.add_subparsers(dest="subcommand")

    # run subcommand
    run_parser = subparsers.add_parser(
        "run",
        help="Run a prompt against the live LLM using a fixture file.",
    )
    run_parser.add_argument("prompt_id", help="Prompt ID (e.g. failure_classifier)")
    run_parser.add_argument(
        "--fixture",
        metavar="PATH",
        help="Path to fixture JSON file providing fmt_vars.",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        dest="dry_run",
        help="Format the prompt and print it; skip the LLM call.",
    )
    run_parser.add_argument(
        "--reload",
        action="store_true",
        default=False,
        help="Force registry.reload() before running (picks up YAML edits instantly).",
    )
    run_parser.add_argument(
        "--runs",
        type=int,
        default=1,
        metavar="N",
        help="Number of times to call the LLM (for stability testing). Default: 1.",
    )
    run_parser.add_argument(
        "--persona",
        metavar="PERSONA",
        default=None,
        help="Persona for overlay resolution (e.g. tpm, pm). Overrides fixture persona.",
    )
    run_parser.add_argument(
        "--expected",
        metavar="PATH",
        default=None,
        help="Path to expected JSON output; diff actual vs expected after a single run.",
    )
    run_parser.add_argument(
        "--show-full-prompt",
        action="store_true",
        default=False,
        dest="show_full_prompt",
        help="Print the full formatted prompt text (not truncated to 500 chars).",
    )

    # docs subcommand
    docs_parser = subparsers.add_parser(
        "docs",
        help="Generate docs/wiki/authorskill-prompts.md from the YAML prompt store.",
    )
    docs_parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help=f"Output path (default: {_DOCS_OUTPUT})",
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list:
        return cmd_list(args)

    if args.subcommand == "run":
        return cmd_run(args)

    if args.subcommand == "docs":
        return cmd_docs(args)

    # No subcommand and no --list
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
