"""AdbSkillStore — Oracle ADB-backed skill artifact store (DECISION-006 Option A).

Reads and writes KB_SHIM.KBF_SKILL_ARTIFACTS (see migration 005).

Column mapping:
  artifact_id     — "{persona}.{skill_name}.{artifact_type}"   (PK)
  synth_id        — authoring session ID
  persona         — persona slug
  skill_name      — skill slug
  artifact_type   — one of the 4 types defined in _base.py
  rel_path        — relative filesystem path (for kb-cli export-skills)
  content         — CLOB with full artifact text
  status          — 'draft' | 'promoted' | 'archived'
  created_at / updated_at — TIMESTAMP

When pool=None (stub mode), all operations are safe no-ops.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from ._base import ARTIFACT_TYPES, SkillStore, make_artifact_id

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL templates (Oracle syntax)
# ---------------------------------------------------------------------------

_SQL_UPSERT = """
    MERGE INTO KB_SHIM.KBF_SKILL_ARTIFACTS tgt
    USING DUAL ON (tgt.artifact_id = :artifact_id)
    WHEN MATCHED THEN UPDATE SET
        synth_id      = :synth_id,
        content       = :content,
        status        = :status,
        updated_at    = :updated_at
    WHEN NOT MATCHED THEN INSERT
        (artifact_id, synth_id, persona, skill_name, artifact_type,
         rel_path, content, status, created_at, updated_at)
    VALUES
        (:artifact_id, :synth_id, :persona, :skill_name, :artifact_type,
         :rel_path, :content, :status, :created_at, :updated_at)
"""

_SQL_READ = """
    SELECT content
    FROM KB_SHIM.KBF_SKILL_ARTIFACTS
    WHERE artifact_id = :artifact_id
"""

_SQL_PROMOTE = """
    UPDATE KB_SHIM.KBF_SKILL_ARTIFACTS
    SET    status     = 'promoted',
           updated_at = :updated_at
    WHERE  persona    = :persona
      AND  skill_name = :skill_name
"""

_SQL_LIST_ALL = """
    SELECT persona, skill_name, status,
           COUNT(*) AS artifact_count,
           MAX(updated_at) AS updated_at
    FROM KB_SHIM.KBF_SKILL_ARTIFACTS
    GROUP BY persona, skill_name, status
    ORDER BY MAX(updated_at) DESC
"""

_SQL_LIST_EXISTING_TYPES = """
    SELECT artifact_type
    FROM KB_SHIM.KBF_SKILL_ARTIFACTS
    WHERE persona    = :persona
      AND skill_name = :skill_name
"""

_SQL_DELETE_SKILL = """
    DELETE FROM KB_SHIM.KBF_SKILL_ARTIFACTS
    WHERE persona    = :persona
      AND skill_name = :skill_name
"""

_SQL_LIST_PERSONA = """
    SELECT persona, skill_name, status,
           COUNT(*) AS artifact_count,
           MAX(updated_at) AS updated_at
    FROM KB_SHIM.KBF_SKILL_ARTIFACTS
    WHERE persona = :persona
    GROUP BY persona, skill_name, status
    ORDER BY MAX(updated_at) DESC
"""

_SQL_UPSERT_PB = """
    MERGE INTO KB_SHIM.KBF_PERSONA_BUILDERS tgt
    USING DUAL ON (tgt.persona = :persona AND tgt.kb_name = :kb_name)
    WHEN MATCHED THEN UPDATE SET
        content_yaml = :content_yaml,
        status       = :status,
        updated_at   = :updated_at
    WHEN NOT MATCHED THEN INSERT
        (persona, kb_name, content_yaml, status, schema_version, created_at, updated_at)
    VALUES
        (:persona, :kb_name, :content_yaml, :status, 1, :created_at, :updated_at)
"""

_SQL_LIST_PB_ALL = """
    SELECT persona, kb_name, content_yaml, status, updated_at
    FROM KB_SHIM.KBF_PERSONA_BUILDERS
    ORDER BY updated_at DESC
"""

_SQL_LIST_PB_PERSONA = """
    SELECT persona, kb_name, content_yaml, status, updated_at
    FROM KB_SHIM.KBF_PERSONA_BUILDERS
    WHERE persona = :persona
    ORDER BY updated_at DESC
"""

_SQL_LIST_PB_STATUS = """
    SELECT persona, kb_name, content_yaml, status, updated_at
    FROM KB_SHIM.KBF_PERSONA_BUILDERS
    WHERE status = :status
    ORDER BY updated_at DESC
"""

_SQL_LIST_PB_PERSONA_STATUS = """
    SELECT persona, kb_name, content_yaml, status, updated_at
    FROM KB_SHIM.KBF_PERSONA_BUILDERS
    WHERE persona = :persona
      AND status  = :status
    ORDER BY updated_at DESC
"""

# Maps artifact_type → relative path template
_REL_PATH_TEMPLATES: dict[str, str] = {
    "workflow_skill":         "framework/workflow_skills/{persona}/{skill_name}.yaml",
    "persona_builder_delta":  "framework/persona_builders/{persona}.yaml.new_kb",
    "eval_extraction":        "eval/gold_sets/{persona}-{skill_name}-extraction.jsonl",
    "eval_workflow":          "eval/gold_sets/{persona}-{skill_name}-workflow.jsonl",
}


def _rel_path(persona: str, skill_name: str, artifact_type: str) -> str:
    return _REL_PATH_TEMPLATES[artifact_type].format(
        persona=persona, skill_name=skill_name
    )


class AdbSkillStore(SkillStore):
    """Oracle ADB-backed skill artifact store.

    pool: oracledb connection pool (synchronous). When None, all operations are
    no-ops — stub mode for dev/testing without a live ADB connection.
    """

    def __init__(self, pool) -> None:
        self._pool = pool

    @staticmethod
    def _now() -> datetime:
        return datetime.now(tz=timezone.utc)

    @staticmethod
    def _install_dict_rowfactory(cur) -> None:
        cols = [d[0].lower() for d in cur.description]
        cur.rowfactory = lambda *vals: dict(zip(cols, vals))

    # ------------------------------------------------------------------
    # SkillStore interface
    # ------------------------------------------------------------------

    def write_artifacts(
        self,
        synth_id: str,
        persona: str,
        skill_name: str,
        artifacts: dict[str, str],
    ) -> None:
        if self._pool is None:
            log.warning(
                "AdbSkillStore: no pool — write_artifacts is a no-op (stub mode)"
            )
            return

        for artifact_type, content in artifacts.items():
            if artifact_type not in ARTIFACT_TYPES:
                raise ValueError(
                    f"Unknown artifact_type {artifact_type!r}; "
                    f"expected one of {sorted(ARTIFACT_TYPES)}"
                )

        now = self._now()

        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                for artifact_type, content in artifacts.items():
                    art_id = make_artifact_id(persona, skill_name, artifact_type)
                    rel = _rel_path(persona, skill_name, artifact_type)
                    params = {
                        "artifact_id":   art_id,
                        "synth_id":      synth_id,
                        "persona":       persona,
                        "skill_name":    skill_name,
                        "artifact_type": artifact_type,
                        "rel_path":      rel,
                        "content":       content,
                        "status":        "draft",
                        "created_at":    now,
                        "updated_at":    now,
                    }
                    cur.execute(_SQL_UPSERT, params)
                    log.debug(
                        "AdbSkillStore.write: artifact_id=%s synth_id=%s",
                        art_id, synth_id,
                    )
            conn.commit()

        log.info(
            "AdbSkillStore.write_artifacts: persona=%s skill=%s count=%d synth_id=%s",
            persona, skill_name, len(artifacts), synth_id,
        )

    def read_artifact(
        self,
        persona: str,
        skill_name: str,
        artifact_type: str,
    ) -> str | None:
        if self._pool is None:
            return None

        art_id = make_artifact_id(persona, skill_name, artifact_type)

        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(_SQL_READ, {"artifact_id": art_id})
                self._install_dict_rowfactory(cur)
                row = cur.fetchone()

        if row is None:
            log.debug("AdbSkillStore.read: not found — artifact_id=%s", art_id)
            return None

        raw = row["content"]
        # oracledb may return a LOB object; materialise it to str if needed
        if hasattr(raw, "read"):
            return raw.read()
        return raw

    def promote(self, persona: str, skill_name: str) -> None:
        if self._pool is None:
            log.warning("AdbSkillStore: no pool — promote is a no-op (stub mode)")
            return

        now = self._now()
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(_SQL_PROMOTE, {
                    "updated_at": now,
                    "persona":    persona,
                    "skill_name": skill_name,
                })
            conn.commit()

        log.info(
            "AdbSkillStore.promote: persona=%s skill=%s", persona, skill_name
        )

    def list_skills(self, persona: str | None = None) -> list[dict]:
        if self._pool is None:
            return []

        sql = _SQL_LIST_PERSONA if persona else _SQL_LIST_ALL
        params = {"persona": persona} if persona else {}

        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                self._install_dict_rowfactory(cur)
                rows = cur.fetchall()

        results: list[dict] = []
        for row in rows:
            results.append({
                "persona":        row["persona"],
                "skill_name":     row["skill_name"],
                "status":         row["status"],
                "artifact_count": int(row["artifact_count"]),
                "updated_at":     str(row["updated_at"]) if row["updated_at"] else "",
            })
        return results

    def delete(self, persona: str, skill_name: str) -> list[str]:
        if self._pool is None:
            log.warning("AdbSkillStore: no pool — delete is a no-op (stub mode)")
            return []

        params = {"persona": persona, "skill_name": skill_name}

        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                # Capture which artifact_types exist before deleting
                cur.execute(_SQL_LIST_EXISTING_TYPES, params)
                self._install_dict_rowfactory(cur)
                rows = cur.fetchall()
                deleted_types = [r["artifact_type"] for r in rows]

                if deleted_types:
                    cur.execute(_SQL_DELETE_SKILL, params)
            conn.commit()

        log.info(
            "AdbSkillStore.delete: persona=%s skill=%s deleted_types=%s",
            persona, skill_name, deleted_types,
        )
        return deleted_types

    def upsert_persona_builder_kb(
        self,
        persona: str,
        kb_name: str,
        content_yaml: str,
        status: str = "draft",
    ) -> None:
        if self._pool is None:
            log.warning(
                "AdbSkillStore: no pool — upsert_persona_builder_kb is a no-op (stub mode)"
            )
            return

        now = self._now()
        params = {
            "persona":      persona,
            "kb_name":      kb_name,
            "content_yaml": content_yaml,
            "status":       status,
            "created_at":   now,
            "updated_at":   now,
        }

        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(_SQL_UPSERT_PB, params)
            conn.commit()

        log.info(
            "AdbSkillStore.upsert_persona_builder_kb: persona=%s kb_name=%s status=%s",
            persona, kb_name, status,
        )

    def list_persona_builder_kbs(
        self,
        persona: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        if self._pool is None:
            return []

        if persona and status:
            sql = _SQL_LIST_PB_PERSONA_STATUS
            params: dict = {"persona": persona, "status": status}
        elif persona:
            sql = _SQL_LIST_PB_PERSONA
            params = {"persona": persona}
        elif status:
            sql = _SQL_LIST_PB_STATUS
            params = {"status": status}
        else:
            sql = _SQL_LIST_PB_ALL
            params = {}

        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                self._install_dict_rowfactory(cur)
                rows = cur.fetchall()

        results: list[dict] = []
        for row in rows:
            raw_yaml = row["content_yaml"]
            # oracledb may return a LOB object; materialise it to str if needed
            if hasattr(raw_yaml, "read"):
                raw_yaml = raw_yaml.read()
            results.append({
                "persona":      row["persona"],
                "kb_name":      row["kb_name"],
                "content_yaml": raw_yaml,
                "status":       row["status"],
                "updated_at":   str(row["updated_at"]) if row["updated_at"] else "",
            })
        return results
