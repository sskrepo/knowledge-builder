"""AdbSkillStore — Oracle ADB-backed skill artifact store (DECISION-006 Option A).

Reads and writes KB_SHIM.KBF_SKILL_ARTIFACTS (see migration 005).

Column mapping:
  artifact_id     — "{persona}.{skill_name}.{artifact_type}"   (PK)
  synth_id        — authoring session ID
  persona         — persona slug
  skill_name      — skill slug
  artifact_type   — one of the 5 types defined in _base.py
  rel_path        — relative filesystem path (for kb-cli export-skills)
  content         — CLOB with full artifact text
  status          — 'draft' | 'promoted' | 'archived'
  created_at / updated_at — TIMESTAMP

CONTRACT: pool is REQUIRED. ADB is the source of truth — if ADB is unavailable
the app fails to start. There is no "stub mode" / no-op fallback. Constructing
this class with pool=None raises ValueError.
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
    ORDER BY updated_at DESC
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
    ORDER BY updated_at DESC
"""

_SQL_DELETE_PB = """
    DELETE FROM KB_SHIM.KBF_PERSONA_BUILDERS
    WHERE persona = :persona
      AND kb_name = :kb_name
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

    pool: oracledb connection pool (synchronous). REQUIRED — passing None raises
    ValueError. ADB is the source of truth; there is no stub-mode / filesystem
    fallback in production. If ADB is unavailable the app must fail to start,
    not silently degrade.
    """

    def __init__(self, pool) -> None:
        if pool is None:
            raise ValueError(
                "AdbSkillStore: pool is required. ADB is the source of truth — "
                "there is no stub-mode / no-op fallback. If ADB is unavailable, "
                "the app must not start. (Previously, pool=None silently dropped "
                "writes, hiding the synth-tpm-14a54555-class of data-loss bug.)"
            )
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
        """Persist all artifacts for one skill atomically.

        Retries up to 3 times on transient errors (pool exhausted, network blip,
        bastion reconnect, deadlock). Permanent errors (constraint violation,
        ValueError on artifact_type) fail fast — retrying won't help.

        Raises:
            ValueError on unknown artifact_type (caller bug, not transient).
            Exception (final attempt's error) if all retries exhausted — the
            caller (skill_builder.conversation._handle_commit) MUST catch this
            and refuse to advance the session past PREVIEW state. Never report
            "committed" to the user unless this method returns successfully.
        """
        # Fail fast on bad input — retrying won't fix this.
        for artifact_type, content in artifacts.items():
            if artifact_type not in ARTIFACT_TYPES:
                raise ValueError(
                    f"Unknown artifact_type {artifact_type!r}; "
                    f"expected one of {sorted(ARTIFACT_TYPES)}"
                )

        import time
        max_attempts = 3
        backoff_seconds = [0.5, 2.0, 5.0]  # before attempts 2, 3, …
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            now = self._now()
            try:
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
                    "AdbSkillStore.write_artifacts: persona=%s skill=%s count=%d "
                    "synth_id=%s attempt=%d/%d",
                    persona, skill_name, len(artifacts), synth_id, attempt, max_attempts,
                )
                return  # success

            except ValueError:
                # ValueError = bad artifact_type → already filtered above, but
                # if it somehow surfaces from inside, re-raise immediately.
                raise
            except Exception as exc:
                last_exc = exc
                if attempt >= max_attempts:
                    log.error(
                        "AdbSkillStore.write_artifacts: FINAL FAILURE after %d attempts "
                        "(persona=%s skill=%s synth_id=%s) — caller must NOT advance "
                        "session past PREVIEW. err=%s",
                        attempt, persona, skill_name, synth_id, exc,
                    )
                    break
                delay = backoff_seconds[attempt - 1]
                log.warning(
                    "AdbSkillStore.write_artifacts: attempt %d/%d failed "
                    "(persona=%s skill=%s) — retrying in %.1fs. err=%s",
                    attempt, max_attempts, persona, skill_name, delay, exc,
                )
                time.sleep(delay)

        # Exhausted retries — propagate the last exception so the conversation
        # layer keeps the session in PREVIEW state and reports the error to the user.
        assert last_exc is not None
        raise last_exc

    def read_artifact(
        self,
        persona: str,
        skill_name: str,
        artifact_type: str,
    ) -> str | None:
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
        """Promote all draft artifacts for (persona, skill_name) to 'promoted'.

        Raises ValueError if no rows match — promoting a non-existent skill
        is almost always a bug (e.g. silent commit failure earlier in the
        session). Previously this was a silent UPDATE no-op which let
        session synth-tpm-14a54555 reach DONE with nothing in ADB.
        """
        now = self._now()
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(_SQL_PROMOTE, {
                    "updated_at": now,
                    "persona":    persona,
                    "skill_name": skill_name,
                })
                rowcount = cur.rowcount
            conn.commit()

        if rowcount == 0:
            raise ValueError(
                f"AdbSkillStore.promote: 0 rows updated for "
                f"persona={persona!r} skill_name={skill_name!r}. "
                f"The skill is not in KBF_SKILL_ARTIFACTS — promotion cannot "
                f"silently no-op. Check that COMMIT actually wrote to ADB; if "
                f"the upstream session reported 'Committed N' without writing, "
                f"that is the real bug."
            )

        log.info(
            "AdbSkillStore.promote: persona=%s skill=%s rows_updated=%d",
            persona, skill_name, rowcount,
        )

    def list_skills(self, persona: str | None = None) -> list[dict]:
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

    def delete_persona_builder_kb(self, persona: str, kb_name: str) -> bool:
        with self._pool.acquire() as conn:
            with conn.cursor() as cur:
                cur.execute(_SQL_DELETE_PB, {"persona": persona, "kb_name": kb_name})
                deleted = cur.rowcount > 0
            conn.commit()
        log.info(
            "AdbSkillStore.delete_persona_builder_kb: persona=%s kb_name=%s deleted=%s",
            persona, kb_name, deleted,
        )
        return deleted

    def upsert_persona_builder_kb(
        self,
        persona: str,
        kb_name: str,
        content_yaml: str,
        status: str = "draft",
    ) -> None:
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
