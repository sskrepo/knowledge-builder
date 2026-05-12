-- Migration 007: KBF_BUGS user grants for dedicated bug DB (DECISION-009)
--
-- Prerequisites: run `kb-cli setup-bug-user --env <env>` first to create the
-- KBF_BUGS Oracle user.  This migration only applies GRANTs.
--
-- Idempotent: re-running is safe (GRANT is idempotent in Oracle).

GRANT CREATE SESSION TO KBF_BUGS;
GRANT INSERT, SELECT ON KB_SHIM.KBF_BUG_REPORTS TO KBF_BUGS;
GRANT INSERT, SELECT ON KB_SHIM.KBF_AUDIT_RUNS   TO KBF_BUGS;
